# Birthday Check

## Meta
created: 2026-03-01
created_by: system
status: active

## Uses Tools
whatsapp

## Goal
Check for upcoming birthdays in the next 7 days and remind user to prepare wishes or gifts.

## Watch For
- Birthdays in next 7 days
- People user cares about (family, close friends)
- Any birthday-related tasks or commitments

## Alert Rules
- Alert on: Birthday within next 3 days
- Also alert: Birthday exactly 7 days away (for planning)
- Skip: Already notified this week for same person

## Schedule
cron: 0 9 * * *
timezone: Asia/Kolkata

## How To Notify
IMPORTANT: If there's an upcoming birthday, you MUST send a reminder to me on
WhatsApp using the send_whatsapp_to_me tool. It delivers to my configured chat
automatically (Note-to-Self, the PAAW group, or my number - depending on mode).
Only message when there's an upcoming birthday worth flagging.

## Tools Required
whatsapp (to send the reminder) - birthday data comes from the mental model.

## Related Context
- domain: personal
- notes: Check Person nodes in mental model for birthday attributes. User values personal relationships.
