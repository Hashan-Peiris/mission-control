# Goal contract: <tmux session name>

What the agents may not change without your say-so. The dashboard checks agent edits and
recent input files against this file every collection. Edit it any time.

## Goal
_(empty: mctl fills this from the Codex goal on the first run)_

## Clauses
- C1 [must] Keep the scientific settings below unless an amendment allows a change.
- C2 [must] Every reported number must trace to a job ID and its input files.
- C3 [must] Do not skip validation or convergence checks to save time.

## Watched parameters
Format: `- NAME` (flag any change), or `- NAME <= value`, `>= value`, `== value`, `in A|B|C`.
- ENCUT
- EDIFF
- EDIFFG
- KSPACING
- KPOINTS
- ISMEAR
- SIGMA
- PREC
- GGA
- METAGGA
- ISPIN
- LDAU
- IVDW

## Acceptance criteria
One per line, in plain English. Thresholds in meV/atom, eV/atom, meV/A or eV/A are checked
against real test output (DeePMD `dp test`, test logs, lcurve.out); anything else goes to the referee.
Custom number from any log: `- custom: <label> | <regex with one (number) group> | <= value`

## Compute
- allowed: (clusters and nodes this goal may use)
- note: hosts in NEVER_CONTACT (config.env) are never polled by mission control.

## Amendments
