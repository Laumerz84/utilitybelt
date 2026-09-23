KitchenMind / Claude Code notification sounds

SOUND      NOTES        LEN  MEANING
--------------------------------------------------------------------------
done       D3 A3      0.68s  Claude finished a turn
attention  A3 A3      0.35s  Blocked on you - permission or a question
error      A3 G#3     0.47s  A tool failed
denied     D3         0.27s  Permission refused
compact    A3 F3      0.53s  Context was compacted
subagent   A3         0.29s  A subagent finished

Why they sound like that
  done       Rising fifth. Rising reads as ready.
  attention  One pitch twice. Repetition reads as waiting. Loudest.
  error      Falling semitone, the only dissonance. Falling reads as failed.
  denied     One low thud, no pitch movement. Nothing is pending.
  compact    Descending third, quiet. It happened on its own.
  subagent   Single light tap. The real done still comes later.

When they do NOT play
  - Turns under 12s are silent (QUIET_UNDER_S in notify.py) - you were
    still watching the screen.
  - Everything is silent while the Claude app is focused
    (MUTE_WHEN_FOCUSED) - the sidebar already brightens. Add names to
    ALWAYS_PLAY to let a sound through anyway.
  - Failed Read / Glob / Grep / TodoWrite make no sound; failed Bash
    or Edit do (BORING_FAILURES).

Grammar: rising = ready, falling = failed, repeating = waiting,
single = done and nothing needed. Loud wants you; quiet informs you.

Hear one:   python make-chime.py --only error --play
Rules live in decide() in notify.py. Sounds live in PROFILES here.
