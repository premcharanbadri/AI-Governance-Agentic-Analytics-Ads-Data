# Architecture diagrams

Diagrams are written in Mermaid so GitHub renders them and they're versioned with the code.
**Update the matching diagram in the same commit as any architecture change.**

Convention used in every diagram: solid teal = built, dashed gray = planned.

| Diagram | UML type | Question it answers |
|---|---|---|
| [01-components.md](01-components.md) | Component (approximated) | What are the parts, level by level? |
| [02-deployment.md](02-deployment.md) | Deployment (approximated) | Where does each part run? |
| [03-domain-classes.md](03-domain-classes.md) | Class | What are the business entities and how do they relate? |
| [04-generator-activity.md](04-generator-activity.md) | Activity | What does generator Pass 1 do, step by step? |
| [05-agent-sequence.md](05-agent-sequence.md) | Sequence | How will the Level 3 agent answer one question? |

Last updated: 2026-09-29 (Level 0, generator Passes 1–3 complete)
