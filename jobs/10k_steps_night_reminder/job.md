# 10k steps night reminder

## Meta
created: 2026-06-11
created_by: server_room
status: paused

## Uses Skill
birthday_reminder

## Goal
To provide a proactive daily check and motivational reminder at the specified evening time (ideally 10 PM IST) to review my current step count progress against the 10,000-step goal and prompt me to complete any remaining steps before bedtime.

## What To Find
*   The user's total cumulative step count for the current day.
*   A calculation of the percentage completion relative to the 10,000 step target.
*   Identification of the time elapsed since the last major activity spike (to gauge recent effort).

## Delivery
- Format: Concise summary and actionable bullet points.
- Length: Maximum of three sentences.
- Only alert on: If the current step count is below 8,000 steps OR if it is within a defined "bedtime window" (e.g., 9 PM - 11 PM) and the goal has not been met.

## Schedule
cron: 0 18 * * *
timezone: Asia/Kolkata

## How To Notify
Respond in the PAAW chat interface

## Context
Although the scheduled cron time is set for 6:00 PM IST, please prioritize executing the check and reminder based on the user's stated goal timing (10:00 PM IST) to maximize effectiveness. The system must integrate with wearable data sources (e.g., Apple Health/Fitbit API) to accurately pull today's step count. Focus the response not just on *what* the number is, but *what needs to be done* next.