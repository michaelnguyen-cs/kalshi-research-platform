# Lag study pass/fail — written BEFORE any results were seen
Date: 2026-08-10

PASS requires ALL of:
1. Peak cross-correlation lag vs the COMPOSITE >= 500ms, clearly above
   the null-control noise band.
2. Median tradeable gap > taker fee + full spread crossed + 1c margin.
3. >= 20 contracts resting at that price at the moment we'd act.
4. OUT-OF-SAMPLE ex-ante replay profitable after fees, beating both
   control strategies, 95% CI excluding zero, on pooled sessions.

Anything less = FAIL = do not restart live trading on this strategy.
I will not move these numbers after seeing results.
