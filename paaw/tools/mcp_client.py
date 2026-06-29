"""
MCP Client - Connects to and calls MCP servers.

This module handles:
- Starting MCP server processes
- Communicating via stdio JSON-RPC
- Calling tools and returning results
"""

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPServerProcess:
    """A running MCP server process."""
    name: str
    process: asyncio.subprocess.Process
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader


class MCPClient:
    """
    Client for communicating with MCP servers.
    
    Starts server processes and sends JSON-RPC requests over stdio.
    """
    
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.servers: dict[str, MCPServerProcess] = {}
        self._request_id = 0
        self._config = None
    
    def _load_config(self) -> dict:
        """Load MCP server configuration."""
        if self._config is None:
            if self.config_path.exists():
                with open(self.config_path) as f:
                    self._config = json.load(f)
            else:
                self._config = {"mcpServers": {}}
        return self._config
    
    def _packages_dir(self) -> Path:
        """Directory used as the npm --prefix for runtime-installed MCP servers."""
        return Path(
            os.environ.get(
                "MCP_PACKAGES_DIR",
                str(Path(__file__).parent.parent.parent / "mcp"),
            )
        )
    
    async def _ensure_package_installed(self, package: str) -> bool:
        """
        Ensure an npm package is installed for an MCP server.

        MCP servers are NOT baked into the image - they're installed on first use
        into <MCP_PACKAGES_DIR>/node_modules (persisted via a volume). Subsequent
        starts skip the install. Returns True if the package is available.
        """
        packages_dir = self._packages_dir()
        node_modules = packages_dir / "node_modules"
        if (node_modules / package).exists():
            return True

        packages_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Installing MCP package: {package} -> {packages_dir}")

        env = os.environ.copy()
        # The non-root paaw user has no usable HOME, so npm can't write its cache
        # at ~/.npm. Point HOME/cache at a writable location for the install.
        env.setdefault("HOME", "/tmp")
        env.setdefault("npm_config_cache", "/tmp/.npm-cache")
        try:
            process = await asyncio.create_subprocess_exec(
                "npm", "install", "--prefix", str(packages_dir),
                "--no-audit", "--no-fund", "--omit=dev", package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await process.communicate()
            if process.returncode == 0:
                logger.info(f"Installed MCP package: {package}")
                return True
            logger.error(
                f"Failed to install {package}: "
                f"{stderr.decode(errors='ignore')[:500]}"
            )
            return False
        except Exception as e:
            logger.error(f"Error installing MCP package {package}: {e}")
            return False
    
    async def start_server(self, server_name: str) -> bool:
        """Start an MCP server process."""
        if server_name in self.servers:
            logger.debug(f"Server {server_name} already running")
            return True
        
        config = self._load_config()
        server_config = config.get("mcpServers", {}).get(server_name)
        
        if not server_config:
            logger.error(f"Server config not found: {server_name}")
            return False
        
        if not server_config.get("enabled", False):
            logger.warning(f"Server not enabled: {server_name}")
            return False
        
        # Auto-install the server's npm package on first use (no image rebuild).
        package = server_config.get("package")
        if package:
            if not await self._ensure_package_installed(package):
                logger.error(f"Could not install package for {server_name}: {package}")
                return False
        
        command = server_config.get("command")
        args = server_config.get("args", [])
        env_vars = server_config.get("env", {})
        
        # Build environment - expand ${VAR} references
        env = os.environ.copy()
        for key, value in env_vars.items():
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                # Expand environment variable reference
                var_name = value[2:-1]
                env[key] = os.environ.get(var_name, "")
            else:
                env[key] = value
        
        # Add local bin to PATH for uvx
        local_bin = os.path.expanduser("~/.local/bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        
        # Make runtime-installed MCP server binaries resolvable on PATH.
        node_bin = str(self._packages_dir() / "node_modules" / ".bin")
        if node_bin not in env.get("PATH", ""):
            env["PATH"] = f"{node_bin}:{env.get('PATH', '')}"
        
        # Find the command
        cmd_path = shutil.which(command, path=env.get("PATH"))
        if not cmd_path:
            logger.error(f"Command not found: {command}")
            return False
        
        try:
            logger.info(f"Starting MCP server: {server_name} ({cmd_path} {' '.join(args)})")
            
            process = await asyncio.create_subprocess_exec(
                cmd_path,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            
            self.servers[server_name] = MCPServerProcess(
                name=server_name,
                process=process,
                stdin=process.stdin,
                stdout=process.stdout
            )
            
            # Initialize the server
            init_result = await self._send_request(server_name, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "paaw",
                    "version": "0.1.0"
                }
            })
            
            logger.info(f"MCP server started: {server_name}", extra={"init": init_result})
            return True
            
        except Exception as e:
            logger.error(f"Failed to start MCP server {server_name}: {e}")
            return False
    
    async def _send_request(
        self, 
        server_name: str, 
        method: str, 
        params: dict
    ) -> dict:
        """Send a JSON-RPC request to a server."""
        server = self.servers.get(server_name)
        if not server:
            raise RuntimeError(f"Server not started: {server_name}")
        
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params
        }
        
        # Send request
        request_json = json.dumps(request) + "\n"
        logger.debug(f"MCP request: {request_json.strip()}")
        server.stdin.write(request_json.encode())
        await server.stdin.drain()
        
        # Read response with timeout, skipping notifications
        try:
            while True:
                response_line = await asyncio.wait_for(
                    server.stdout.readline(),
                    timeout=30.0
                )
                
                if not response_line:
                    raise RuntimeError(f"No response from server {server_name}")
                
                logger.debug(f"MCP response: {response_line.decode().strip()}")
                response = json.loads(response_line.decode())
                
                # Skip notifications (they don't have an "id" field)
                if "id" not in response:
                    logger.debug(f"Skipping notification: {response.get('method', 'unknown')}")
                    continue
                
                # Check if this is our response
                if response.get("id") == self._request_id:
                    if "error" in response:
                        raise RuntimeError(f"MCP error: {response['error']}")
                    return response.get("result", {})
                    
        except asyncio.TimeoutError:
            raise RuntimeError(f"Timeout waiting for response from {server_name}")
    
    async def call_tool(
        self, 
        server_name: str, 
        tool_name: str, 
        arguments: dict
    ) -> dict:
        """Call a tool on an MCP server."""
        # Ensure server is running
        if server_name not in self.servers:
            started = await self.start_server(server_name)
            if not started:
                return {"error": f"Could not start server: {server_name}"}
        
        try:
            result = await self._send_request(server_name, "tools/call", {
                "name": tool_name,
                "arguments": arguments
            })
            return result
        except Exception as e:
            logger.error(f"Tool call failed: {e}")
            return {"error": str(e)}
    
    async def list_tools(self, server_name: str) -> list[dict]:
        """List available tools from a server."""
        if server_name not in self.servers:
            started = await self.start_server(server_name)
            if not started:
                return []
        
        try:
            result = await self._send_request(server_name, "tools/list", {})
            return result.get("tools", [])
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            return []
    
    async def stop_server(self, server_name: str) -> None:
        """Stop an MCP server process."""
        server = self.servers.pop(server_name, None)
        if server:
            try:
                server.process.terminate()
                await asyncio.wait_for(server.process.wait(), timeout=5.0)
            except:
                server.process.kill()
            logger.info(f"Stopped MCP server: {server_name}")
    
    async def stop_all(self) -> None:
        """Stop all running MCP servers."""
        for name in list(self.servers.keys()):
            await self.stop_server(name)


# Simplified function for one-off tool calls
async def call_mcp_tool(
    server_name: str,
    tool_name: str,
    arguments: dict,
    config_path: Optional[Path] = None
) -> dict:
    """
    Convenience function to call an MCP tool.
    
    Starts the server if needed, calls the tool, returns result.
    Note: For production, you'd want to keep the client alive.
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent.parent / "mcp" / "servers.json"
    
    client = MCPClient(config_path)
    try:
        result = await client.call_tool(server_name, tool_name, arguments)
        return result
    finally:
        await client.stop_all()
