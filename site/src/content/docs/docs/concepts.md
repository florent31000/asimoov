---
title: Concepts
description: Scene, memory, personality, behaviors — the pieces ASIMOOV is made of.
---

## Scene

What is true about the room right now: who is visible, who is speaking, where
they are, how far. The scene is rebuilt continuously from percepts and is the
only thing the rest of the system reads about the present.

## Memory

What survives a reboot: people, facts about them, episodes, a journal. One
SQLite file you can open, read, back up or delete.

## Personality

A `persona.yaml`: identity, traits, speaking style, relationships, initiative
settings, hard rules. Readable, editable, shareable.

## Behaviors

Named, high-level things a robot can do — *wave hello*, *shake hands*,
*look at Sam*. Each body declares which ones it supports and translates them
its own way.

## Body

An adapter: one class, nine methods, and a manifest declaring capabilities,
gestures and safety. See [Bodies](/docs/bodies).

## Contracts

The dataclasses, ABCs and JSON Schemas every part of ASIMOOV agrees on. They
are frozen; changes are additive only. See [Contracts](/docs/contracts).
