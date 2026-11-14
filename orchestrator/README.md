# Controls Project Orchestrator

Give it a project specification. A team of Claude agents, hosted by Anthropic (Claude Managed Agents), then:

1. **Phase 1 (platform selection):** extracts the requirements, researches Siemens, Rockwell and another platform in parallel, and recommends one. It also writes the I/O list and a preliminary bill of materials.
2. **You approve** the platform or choose a different one.
3. **Phase 2 (development):** writes the functional design, the PLC code (SCL for Siemens, Structured Text for Rockwell or IEC 61131-3), the tag tables, the alarm list, a simulation test plan and draft supplier emails.

Each phase is graded against a checklist, and the agents revise their work until it passes. Nothing is emailed or sent anywhere.

## The team

| Agent | Model | Job |
|---|---|---|
| Controls lead engineer | Claude Opus 5.5 | Reads the spec, plans, delegates, checks everything, writes the summary |
| Vendor researcher | Claude Sonnet 5.5 | Looks up vendor sites and datasheets and cites a source for every fact |
| PLC developer | Claude Opus 5.5 | Writes reusable function blocks and tag tables, and checks I/O coverage with a script |

Web access is limited to manufacturer and standards sites (Siemens, Rockwell, Beckhoff, Schneider, ABB, ODVA, PROFIBUS/PROFINET, ISA, and others).

## One-time setup

1. Install the `ant` CLI (version 1.40 or later): <https://github.com/anthropics/anthropic-cli/releases>
2. Sign in: `ant auth login`, or set `ANTHROPIC_API_KEY`
3. From this folder, create the agents:

   ```bash
   ant apply --dry-run -v agents/controls-lead.md agents/vendor-researcher.md agents/plc-developer.md environments/controls-env.yaml
   ant apply agents/controls-lead.md agents/vendor-researcher.md agents/plc-developer.md environments/controls-env.yaml
   ```

   This writes `claude-lock.json`, which holds the agent IDs. Commit it. To change an agent later, edit its file and run `ant apply` again.
4. `pip install anthropic`

## Run a project

```bash
python run.py spec.pdf io_schedule.xlsx --title "Pump station upgrade" --budget 40
```

- Watch it live with the link it prints, or in the terminal.
- Files are saved in `projects/<title>/`.
- `--phase1-only` stops after the recommendation. `--platform "Siemens S7-1500"` skips the approval question.
- `--download SESSION_ID` fetches an earlier run's files again.
- When the budget runs out, it asks whether to add more. `Ctrl+C` stops it; the work done so far stays in the session.

**Cost:** you set the cap with `--budget`. Expect roughly $10–40 per project, depending on the spec's size.

## Before anything touches real equipment

- Import, compile and test the code in the vendor's simulator first: PLCSIM (Siemens), Emulate (Rockwell) or CODESYS simulation.
- Safety functions are left as marked placeholders by design. A qualified safety engineer must design and validate them on a safety-rated controller.
- Verify part numbers and prices with your distributor.

## Files

```
agents/          agent definitions (frontmatter = settings, body = instructions)
environments/    the sandbox the agents run in (network allow-list)
run.py           runs a project: upload, phase 1, approval, phase 2, download
test_run.py      offline tests: python -m pytest test_run.py
```
