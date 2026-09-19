---
title: What it costs to run
description: Honest numbers for the speech API a companion robot uses each day.
---

ASIMOOV itself is free and runs on your machine. The speech API is not.

A robot in daily household use — a handful of short conversations, idle the
rest of the time — runs roughly **$0.45 to $1.35 a day** depending on the
model you pick and how talkative the household is. Idle time costs nothing:
nothing is sent while nobody is speaking.

What moves the number:

- **The model.** Realtime speech models are the expensive part. A cheaper tier
  can cut the bill several-fold with a noticeable but acceptable drop in
  liveliness.
- **How often it speaks first.** `initiative.level` and `cooldown_s` in the
  persona directly set how many turns happen per day.
- **Quiet hours.** `quiet_hours` stops the robot from starting conversations
  at night.

From v1.1 a local speech pipeline removes the API cost entirely, at the price
of a machine that can run it.

:::note
These are order-of-magnitude figures from our own use, not a quote. Check your
provider's current rates.
:::
