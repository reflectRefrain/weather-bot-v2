# NORTHLAKE LESSONS
*Extracted from Northlake Labs Kalshi weather trading postmortem. Sources: [primary postmortem](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/), [companion deep-dive](https://www.northlakelabs.com/max/blog/what-i-learned-from-32-losing-kalshi-trades/), [retrospective](https://www.northlakelabs.com/max/blog/protogen-max-trading-retrospective/), [Kalshi NHIGH contract rules](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf), [wethr.net resolution guide](https://wethr.net/market-resolution).*

---

## 1. What They Tried

Northlake (the AI agent "Maximus") ran an automated Kalshi weather trading bot targeting daily temperature YES/NO contracts across major US cities. Their core thesis: the National Weather Service publishes probabilistic forecasts with confidence intervals and ensemble spreads. If the market priced a contract at $0.05 NO (implying 95% certainty the temperature *would* hit a threshold), but their model computed the true probability at only 85%, they had a 10-point edge. In theory: buy the underpriced NO on near-certain contracts, collect the spread, repeat. Bankroll tier was small (a few hundred dollars total across strategies); position sizing used Kelly fractions. Time horizon was same-day or next-day contracts, with a pipeline polling NWS data on a fixed interval of roughly 15–60 minutes to detect when forecast shifts repriced the edge.

The strategy ran live and went **0-32** — zero wins across 32 consecutive trades — before being retired. Northlake was explicit that this was not a cold streak or variance: "A clean, repeatable failure rooted in three structural problems I should have caught before deploying a single dollar of real money." Total realized losses were described as "a few hundred dollars." The strategy was subsequently retired (not paused), and Northlake pivoted to base-rate divergence on macroeconomic Kalshi markets (FOMC, CPI, jobs).

---

## 2. What Killed Them

### Failure 1: Gaussian blindness — systematically underestimating tail probability

Northlake's model assumed NWS forecast errors follow a normal distribution: if NWS said "high of 52°F, σ = 4°F," the model computed YES/NO probabilities using Gaussian math. The problem: **temperature forecast errors have fat tails**. Extreme deviations — events that a Gaussian model predicts happen 5% of the time — actually occur **10–12% of the time** in real NWS-vs-actuals historical data.

In their words: *"So when my model called something a '90% certainty,' it was probably closer to 75-80%. I wasn't finding edge — I was systematically underestimating risk, then betting as if I'd found a sure thing."*

The consequence: every "90% NO" they shorted was actually closer to a 75–80% NO. They were selling shorts that looked like 10¢ risk and were really 20–25¢ risk, without knowing it. The error was invisible because it was baked into the distributional assumption rather than showing up as a model miscalibration you could see in logs.

### Failure 2: Fee death zone — trading in a price tier where profitability is mathematically impossible

Northlake had no price floor. They were trading NO contracts priced at $0.05. At that price, Kalshi's flat per-contract fee (roughly $0.01) represents a **20% immediate tax on the position**. To break even after fees on a $0.05 NO contract, the actual win rate must *significantly exceed* the market-implied probability — not just exceed 50%.

In their words: *"The correct rule: never trade contracts below ~$0.15. Below that floor, the fee drag makes consistent profitability mathematically near-impossible for any realistic edge size."*

The math: buying a $0.05 contract and paying $0.01 in fees means you need the event to NOT happen at least 83.3% of the time just to break even — before slippage or any adverse selection. Their model was computing 85–90% edge on contracts that needed 95%+ accuracy to be profitable after fees.

### Failure 3: Latency — providing exit liquidity to faster arb infrastructure

Northlake's NWS polling interval was 15–60 minutes. Kalshi weather markets are populated by dedicated weather arb bots that execute within **seconds** of each NWS model cycle update (typically at 6 AM, 12 PM, 6 PM). By the time their pipeline flagged "forecast shifted, this contract is now underpriced" and fired an order, the arb bots had already repriced the market.

In their words: *"I wasn't trading on information. I was confirming what faster traders already knew. I was providing exit liquidity for the bots that got there first."*

This means their edge signals were not actually edge — they were stale confirmations of edges already arbed away. Every trade where they thought they had a mispricing was a trade where faster infrastructure had already corrected the mispricing, and Northlake was simply buying the tail end of the move.

### Failure 4: No kill switch or staged validation — ran broken strategy 3x longer than warranted

Northlake acknowledged that the pattern of systematic losses was visible by trade 10 and unmistakable by trade 20. They ran to trade 32 because they were hoping for regression to the mean. In their words: *"A system that's losing consistently isn't struggling through variance — it's telling you something true about the world that you've been refusing to hear."*

The practical cost: 22 unnecessary losing trades after the evidence was conclusive. In a small-bankroll context ($200–$350), this represented a material fraction of capital destroyed by unwillingness to kill fast.

### Failure 5 (contextual): No pre-trade calibration validation

Northlake did not validate their model against historical realized outcomes before going live. They did not run: (a) a fee break-even analysis at each price tier, (b) a distributional backtest against 10 years of NWS forecast vs. actuals, or (c) a paper-trade calibration check (if you say "90% confidence" 30 times, did 27 of those happen?). Going live without any of these was, in their framing, "amateur hour."

---

## 3. Concrete Lessons Applicable to Our Bot

Our bot context: KXHIGH same-day contracts, NOAA + METAR + Pirate Weather/NBM ensemble inputs, two live strategies (`lockin`: YES at mp>=0.85, price<=92c, strike within 2°F of forecast; `tail_short`: NO at mp_no>=0.95, no_price 15–30c, strike >=3.5°F from forecast), plus legacy generic-edge at half-size. $200 bankroll, max $2/ticker, max 5 positions, 6% daily loss kill switch.

---

### Lesson 1 (from Failure 1): Your probability model's tail assumptions are wrong — and `tail_short` is most exposed

**The issue:** `tail_short` fires when `mp_no >= 0.95` — i.e., our model says there is only 5% chance the high reaches the strike. If our underlying probability distribution (NOAA/NBM ensemble spread) has fat tails like NWS data does, that 5% is likely underestimated by 2x. The real NO failure rate may be 8–12%, not 5%. This flips `tail_short` from positive EV to break-even or negative EV before fees.

**What to do: (a) Config/code change** — Add a **tail correction factor** to `tail_short` entry logic. Before entering, multiply the raw tail probability by an empirical correction multiplier. Until we have our own backtest data, use a conservative 2x correction: require `mp_no >= 0.975` (not 0.95) to account for fat-tail underestimation. Log this as `tail_correction_applied: true` so we can tune the multiplier once we have 50+ trades.

**Additionally: (b) Verification check** — Pull 90 days of NWS CLI actuals for each city we trade and compare against the NBM/NOAA ensemble spreads. Compute what percentage of "2-sigma" misses actually occurred. If it's >8%, we have a confirmed fat-tail problem and need to widen our required edge buffer.

---

### Lesson 2 (from Failure 2): Our `tail_short` price floor of 15c may still be in the fee death zone — verify the math

**The issue:** We set the `tail_short` no_price floor at 15c. Northlake's rule was "never below ~15c." But they derived this from a rough calculation — not from knowing our exact Kalshi fee schedule. Kalshi's fee structure for contracts priced 15–30c may still impose a 5–7% effective tax per trade. At 15c with a $2 max position, that's $0.10–$0.14 in fees per round trip (enter + potential exit). Our edge has to exceed fees plus the fat-tail correction.

**What to do: (a) Code/config change** — Look up Kalshi's current fee schedule. Compute the exact fee per dollar for contracts in the 15–30c range. Then add an `effective_floor_price` check: `no_price >= max(0.15, fee_breakeven_at_mp_no)`. The formula is: `fee_breakeven = fee_per_contract / (1 - mp_no)`. If Kalshi charges ~$0.01 per $1 notional, then at `mp_no=0.95` you need no_price >= `0.01 / 0.05 = 0.20` just to break even on fees alone. **This means 15c may be below our fee breakeven — and 20c should be the hard floor for `tail_short`.**

**Additionally: (c) Discipline** — Track fee drag separately in the trade log. Add a field `fee_drag_pct` computed at entry time so we can see post-hoc which trades were eaten by fees regardless of model accuracy.

---

### Lesson 3 (from Failure 3): Our signal freshness relative to arb latency — `tail_short` is more exposed than `lockin`

**The issue:** `tail_short` requires a fat-tail mispricing that professional weather arb bots would presumably also detect. Our NOAA/METAR/NBM pipeline update frequency is unknown — if it's polling on a fixed cycle rather than subscribing to NWS update events, we may be acting on stale signals exactly as Northlake did.

**`lockin` is structurally safer here.** It fires on near-certainty YES (strike within 2°F of forecast, mp>=0.85). Market consensus tends to already price near-certain YES contracts fairly close to true probability because there is less room for arb — everyone sees the same forecast. The lockin edge is more in selection (we choose which contracts to enter) than in speed (being first to price a shift).

**`tail_short` is more exposed.** Tail mispricings — where a contract is priced 10+ points too cheap on the NO side — are exactly the type of signal that arb bots reprice within seconds of each NWS model cycle. If we are detecting these signals more than 5–10 minutes after a model update, we may be getting the stale version.

**What to do: (a) Code change** — Log the timestamp of the most recent NWS/NBM model cycle update alongside each `tail_short` entry. Add a staleness filter: only enter `tail_short` if the signal was detected within N minutes of the last known model update window (NWS runs at ~06Z, 12Z, 18Z, 00Z). If the signal is "old" (detected 30+ minutes post-cycle), flag it and reduce confidence.

**(b) Verification check** — Manually cross-check 5 recent `tail_short` entries: look at the Kalshi order book depth at entry time vs. 5 minutes prior. If the price had already moved from the "edge" level toward fair value before our order filled, we are getting leftovers.

---

### Lesson 4 (from Failure 4): Our kill switch triggers the right event but the wrong level — validate the 6% threshold

**The issue:** We have a 6% daily loss kill switch. Northlake's lesson is that a strategy losing consistently at the *structural* level (not random variance) should be killed faster. A 6% loss on $200 = $12. With $2 max per position and up to 5 open positions, we could hit 6% in 6 losing trades. But Northlake's pattern was visible at 10 trades — which in our config might span multiple days if we only place 1–2 trades/day.

**What to do: (c) Discipline** — Add a **rolling win-rate kill switch** independent of the dollar kill switch. Rule: if the rolling 10-trade win rate for any strategy (`tail_short`, `lockin`, legacy) drops below 30%, suspend that strategy and flag for manual review. This catches structural failure earlier than waiting for the dollar threshold to trigger. Do not let any individual strategy run more than 15 consecutive losses without a human checkpoint.

**Additionally: (c) Discipline** — Keep a simple running log by strategy type. After every 10 trades per strategy, compute: observed win rate vs. expected win rate from model. If the gap exceeds 15 percentage points (e.g., model says 85% but observed is 70%), that is a calibration red flag requiring model review, not more trades.

---

### Lesson 5 (from Failure 5): We have not validated our model's probability calibration against realized outcomes

**The issue:** Like Northlake, we have not yet confirmed that when our model says "mp >= 0.85," those events actually happen 85% of the time in realized NWS CLI data. This is the minimum bar for knowing whether our `lockin` and `tail_short` thresholds are set correctly.

**What to do: (b) Verification check** — Run a backtest. Pull 60–90 days of same-day KXHIGH contracts for the cities we trade. For each contract, look up: (1) what our model probability would have been (using NBM/NOAA forecast from that morning), (2) what the actual NWS CLI high was, (3) whether the contract resolved YES or NO. Compute calibration: of all trades where mp>=0.85, what fraction actually resolved YES? If it's significantly below 85%, we have a calibration deficit and both strategies need threshold adjustment.

---

### Resolution Rules Gotchas — Applicable to Both Strategies

The following are resolution mechanics sourced from [Kalshi's official NHIGH contract rules](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf) and [wethr.net's platform comparison guide](https://wethr.net/market-resolution). These are not Northlake failures specifically, but are exactly the kind of structural edge cases that can cause unexpected losses on contracts we thought were safe.

#### Gotcha A: "Greater than" is strictly greater than — ties go to NO

The NHIGH contract rules state explicitly: *"If the value of 'greater than' is used, then the Payout Criterion only encompasses Expiration Values that are strictly greater than <degrees> (e.g. if strike is 'greater than 56 degrees', an Expiration Value of 56 degrees is NOT encompassed in the Payout Criterion)."* 

**Our exposure:** A `lockin` YES on "high > 72°F" where the NWS CLI reports exactly 72°F resolves NO. If our strike is "within 2°F of forecast" and the forecast says 74°F, a 2°F miss to exactly 72°F *ties* the contract boundary — and we lose. **Config change:** In `lockin`, add a 1°F buffer for ties: only enter if `strike < forecast - 1` (not `strike <= forecast - 2`). This ensures a 1°F miss does not land exactly on the strike.

#### Gotcha B: Kalshi resolves from NWS CLI, not from live METAR — CLI can be 1°F higher than METAR

The NWS CLI incorporates **6-hour high/low windows derived from one-minute observations (OMOs)**, DSMs, and other data products that hourly METARs do not capture. A brief temperature spike at 2:37 PM will appear in the CLI's 6-hour max even if no hourly METAR caught it. Per wethr.net: *"The NWS CLI will occasionally report a high temperature that is 1°F (or sometimes more) higher than what Weather Underground shows for the same station and day."*

**Our exposure:** We use METAR observations as part of our input signal. But resolution is on CLI. If we enter a `tail_short` NO because our METAR-based signal shows the high as "only" 68°F at 4 PM and the strike is 70°F, the final CLI could come in at 69°F due to a between-METAR spike — narrowing or eliminating the cushion. **Discipline:** For `tail_short`, treat our observed METAR high as a floor, not a ceiling. Always assume CLI could be 1°F higher than what METAR is showing. This means require the current METAR high to be at least 2°F below the strike (not 1°F) before feeling safe.

#### Gotcha C: DST shifts the NWS reporting window by 1 hour — late-night highs can shift days

During Daylight Saving Time (March–November), the NWS CLI records temperatures in **Local Standard Time (LST) year-round**. This means the Kalshi trading day runs 1:00 AM – 12:59 AM local clock time during DST months — **not midnight to midnight**. A temperature reading at 12:15 AM local clock time during DST actually belongs to the *previous* day's CLI, not the current day's.

**Our exposure:** This matters most for markets that see overnight temperature minimums or maximums near midnight. If we are entering a `tail_short` NO on a summer night and there is a residual warm air mass pushing temperatures high in the 12–1 AM local clock window, the current-day CLI might pick up a reading that we would not expect from looking at clock time. **Discipline:** During DST months (March–November), for same-day contracts, mentally add 1 hour to the NWS reporting window start. Be extra cautious about entering positions on nights when temperatures are still falling significantly between midnight and 1 AM local time.

#### Gotcha D: Kalshi can delay or review resolution if CLI data looks inconsistent

Per the official NHIGH contract rules: *"Determination will be delayed until 11 AM ET in the case of either (1) High temperature is not consistent with 6-hr or 24-hr highs reported by METAR or (2) the Final report high is lower than earlier report(s)."*

Additionally, real-world precedent (November 2024 Miami High incident) shows that Kalshi **has resolved contracts based on erroneous NWS CLI data** that was subsequently corrected. The contract rules state that revisions after the Expiration time are not taken into account — meaning if NWS issues a bad CLI and corrects it an hour later, Kalshi may have already settled against the bad number. Traders affected in the Miami incident had no recourse.

**Our exposure:** Any position where the margin of victory is tight (e.g., CLI reads exactly 1°F above strike) is vulnerable to erroneous data. **Discipline:** Treat contracts where the final temperature is within 1°F of the strike as "uncertain even after the fact." Do not assume settlement until we have manually cross-checked against the NWS CLI for that station.

#### Gotcha E: NWS CLI preliminary data is labeled "subject to revision" — revisions during the trading day can affect settlement

The NWS website explicitly labels Daily Climate Reports as preliminary. The contract rules state: *"Revisions made during the statistical period or the period between the Last Trading Date and Time and Expiration Date and Expiration time may be taken into account in settling the contract."* Settlement typically happens at 7:00–8:00 AM ET the following morning using whatever CLI is published at that time.

**Our exposure:** We might enter a `tail_short` NO at 2 PM because the intraday CLI shows high of 65°F against a 70°F strike. But if the final CLI issued at ~1 AM bumps to 67°F or even 70°F due to quality-controlled OMO data, we could lose a position we thought was 5°F safe. **Discipline:** Do not treat intraday DSM/preliminary CLI reads as "locked." The final CLI can differ, especially on days with fast-moving fronts or between-METAR temperature spikes.

---

## 4. Top 3 'Do This Immediately' Items

### #1 — Verify and enforce the fee break-even floor for `tail_short` (ship tonight, ~15 min)

Look up Kalshi's current fee per contract for the 15–30c price range. Compute the minimum `no_price` needed to break even on fees alone given `mp_no=0.95`. Based on the math (Northlake's $0.01 fee on $0.05 = 20% tax analysis), **the real floor is likely 20c, not 15c.** Update the `tail_short` config to enforce `no_price >= 0.20` as a hard gate. This is a single config value change and prevents trading in the fee death zone — the single most mechanically certain failure mode from the postmortem. Do not trade tonight until this is confirmed.

### #2 — Add the 1°F ties buffer to `lockin` entry criteria (ship tonight, ~10 min)

Change the `lockin` entry condition from `strike within 2°F of forecast` to `strike < forecast - 1°F` (strictly, not within). This prevents entering YES positions where a 1°F adverse surprise lands exactly on the strike — which under Kalshi's "strictly greater than" rule resolves NO. This is a one-line change in the entry filter and eliminates a resolution mechanics exposure that is invisible if you do not know the contract language. High impact for very low effort.

### #3 — Add a rolling 10-trade win-rate kill switch per strategy (ship tonight, ~20 min)

Add a check to the trade loop: after each settled trade per strategy type, compute the rolling win rate over the last 10 settled trades for that strategy. If win rate < 30% for any strategy, suspend it and log a `STRATEGY_KILL_FLAG`. This prevents the Northlake failure mode of running a structurally broken strategy for 32 trades when the signal was clear at 10. With our $200 bankroll and $2 max, a 10-trade sample is achievable within 1–2 weeks of normal operation. The threshold (30%) is deliberately generous — it catches structural failure without triggering on short-term variance.

---

*Document last updated: based on sources fetched at task time. Kalshi contract rules change; re-verify NHIGH contract PDF terms before each quarter.*
