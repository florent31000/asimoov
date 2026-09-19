---
title: Privacy
description: What stays on your machine, what leaves it, and how to erase it.
---

Everything runs on your machine. **There is no ASIMOOV server.** We cannot see
your robot, and we could not if we wanted to.

## Camera

Images are never stored and never sent anywhere. A detected face is turned
into a short vector of numbers on your disk. That vector is what recognition
compares against; the picture it came from is discarded.

## Voice

The only thing that leaves your network is the conversation itself — text and
speech — sent to the voice provider you chose. `VoiceProvider` is an
interface: you can change provider, and from v1.1 you can run one locally.

## Consent

Nobody is recognised by accident. A person is only enrolled when someone says
so out loud, or when you run `asimoov enroll` yourself.

## Erasure

Say **"forget me"** and the record, the facts and the face vector are deleted.
Memory is one SQLite file under `~/.asimoov/`. You can open it, read it, back
it up, or delete it.

Recognising the people in your home means handling personal data. We would
rather you understand exactly what happens than trust us.
