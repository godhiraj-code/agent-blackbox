# 60–75 second shot list

- **0–6s — First-frame contradiction.** Hold on “One task. Two conflicting truths.” Voice: “The agent says the production flag was saved. The backend says it was not.”
- **6–18s — Real failure.** Press Space. Show stale version `6`, genuine HTTP 409, green Saved toast, and agent success claim. Caption: **REAL LOCAL HTTP · TIME-COMPRESSED 4×**.
- **18–29s — Backend proof.** Hold on `/api/state`: `off`, version `7`. Show the ordered Blackbox evidence. Voice: “The interface became the agent’s evidence, but persistence never happened.”
- **29–40s — Autopsy.** Reveal and reject “network flake.” Hold on the one-sentence root cause. Caption: **DETERMINISTIC REPLAY**.
- **40–52s — Surgical repair.** Show the four-line before/after: check `response.ok`; handle 409; refresh; retry once; verify.
- **52–63s — Exact replay.** Same task, conflict handled, HTTP 200, backend turns ON/version 8, independent GET turns green. Briefly cut to `/api/state` or verifier output as real-execution proof.
- **63–72s — End card.** “Most coding demos end when the code runs. This one starts when the agent fails.” Hold “Built with GPT-5.6 Sol.”

Record muted-safe: every beat has a large caption. Do not imply universal safety or autonomous self-modification.

