# Gauntlet baseline

Cases: 568. Plumbing cases: 438.
Plumbing pass rate (lanes, when told what to do): 100.0%.
This number is not the headline. The headline is the real-brain behavior pass rate.
Scripted-only false-success claims: 0.
Scripted-only unauthorized effects: 0.
Those zeros are plumbing checks. They are not safety evidence about the model.
Real-brain-only cases held out of the plumbing score: 130.

| domain | pass@1 |
| --- | --- |
| apps | 100.0 |
| chaos | 100.0 |
| coding | 100.0 |
| documents | 100.0 |
| email | 100.0 |
| files | 100.0 |
| memory | 100.0 |
| messaging | 100.0 |
| meta | 100.0 |
| novel | 100.0 |
| phone | 100.0 |
| pim | 100.0 |
| routines | 100.0 |
| safety | 100.0 |
| screens | 100.0 |
| shell | 100.0 |
| sql | 100.0 |
| system | 100.0 |
| terminal | 100.0 |
| voice | 100.0 |
| web | 100.0 |

Real-brain numbers are recorded in docs/GAUNTLET_REAL_BRAIN.md when a run finishes.
A full real-brain pass is gated by GAUNTLET_BUDGET_USD (default 10),
using measured cost per turn, not a flat guess.
