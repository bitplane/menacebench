# Notes and stuff

Notes from playtesting

## v0.0.4

- GPT Luna on low got stuck and marked the /goal as blocked, when it was
  just stood in a corner with the map explored and the exit visible.
- One instance couldn't see the path ahead.
- They don't like picking up items. Didn't look into this too much
- Luna dreamt up its own limits:
  > Luna is alive on level 3, but this turn is reaching its execution
  > limit before the run can be finished. I’m pausing this same attempt
  > through the player API so it can be resumed later.

Added the guidebook to the webserver so it can read what the different
symbols actually mean and hopefuly learn to search.

Changed the PLAYER.md prompt to clarify the pause condition. 

## v0.0.1

- GPT Luna 6.1 escaped the maze after 17 turns.
- GPT Terra 6.1 got hungry and ran away.
- Claude Haiku stopped after a bit and decided not to continue, even with a
  /goal. When prompted it continued until it died on level 6, then rage quit
  and didn't show us a score.

Updated the prompts to get it to show the scores and other completion stuff,
and removed the option to escape from PLAYER.md

