# Recording runbook

Recordings let anyone replay a scenario offline. Record each scenario once with a real key, check
that it replays, then commit it.

## Before you start

- Put `GEMINI_API_KEY=...` in `.env` (git-ignored). Never paste the key into chat, commits or
  screenshots.
- Free tier: about 20 requests per day per Google Cloud project for gemini-3.8-flash. One scenario
  can use 12-20 calls. A paid key removes the wait: about $1.50-3.50 per full pass of all
  scenarios at the October 2026 introductory price ($0.75 input / $3.75 output per 1M tokens until
  2026-12-31). Set a billing budget alert first.
- Recording only re-asks prompts that have no recording yet, so an interrupted run resumes where
  it stopped.

## Steps (per scenario)

```bash
python run_demo.py brownfield --record --auto-approve     # 1. record (re-run until it SUCCEEDS)
python run_demo.py brownfield --auto-approve              # 2. replay offline, without network or key
python run_demo.py brownfield --auto-approve              # 3. replay again: the outcome must match
```

4. **Prune** recordings from abandoned paths. These are answers to prompts that no longer occur,
   left over from fixed bugs or changed prompts. Run
   `python run_demo.py brownfield --auto-approve --prune`: it replays, and only if the replay
   succeeds it deletes the recordings that run did not use.
5. **Scan** for credentials: `git grep -nE "AIza|AQ\.|GEMINI_API_KEY=" recordings/` must print
   nothing.
6. Run `pytest -q -m slow`, then commit `recordings/<scenario>/`.

## Keeping recordings valid

A recording is keyed by the exact request (system prompt, prompt, temperature, JSON mode). Any
change to an agent's prompt, schema or inputs makes those calls miss and need re-recording.
Recorded scenarios therefore pin their prompts. Only first prompts are guaranteed stable:
feedback-only hints and previous outputs appear only when they apply.

## Order for the .NET scenarios

Brownfield extends the real greenfield output, so record in this order:

```bash
python run_demo.py greenfield --record --auto-approve     # 1. the .NET URL shortener
python -m demo.baseline runs/greenfield                   # 2. snapshot it as brownfield's existing app
python run_demo.py brownfield --record --auto-approve     # 3. add daily analytics to it
python run_demo.py ambiguous --record --auto-approve      # 4. vague request, scripted product-owner answers
```

Then run steps 2-6 above for each scenario, and commit `recordings/` plus
`demo/baselines/url-shortener-dotnet.json`.

## Current status

| Scenario | Recorded |
|---|---|
| greenfield (.NET) | No: step 1 above |
| brownfield (.NET) | No: needs the greenfield baseline first (step 2), then step 3 |
| ambiguous (.NET) | No: step 4 |
| greenfield-python | Yes (12 calls, gemini-3.8-flash) |
| failures | Not needed (scripted) |
| new | Recorded automatically to `recordings/<project>/` whenever you run it with a key |
