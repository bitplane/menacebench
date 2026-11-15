Patches 001–006 are from davidbau/teleport-contest, nethack-c/patches,
retrieved 2026-11-15. Their exact bytes are tracked here. They modify
NetHack under the NetHack General Public License (see LICENSE-NetHack).
Source: https://github.com/davidbau/teleport-contest/tree/main/nethack-c/patches

007 is this harness's additional patch: consumed-key acknowledgments,
distinct exit messages, session RNG reset after restore, and denial of
wizard/explore authorization. Shell, suspend, and mail are disabled by
compiler definitions in the build profile.

008 emits the selected character from game state at input boundaries, including
for attempts that later end by abandonment.
