Follow AGENTS.md at the repository root (AIDD rules with Code Map). In short:
- Call `codemap_impact` before changing a table, API, procedure, job or shared method; if the impact spans 2+
  systems or shared tables/batches/interfaces, stop and propose the cross-system process in docs/changes/.
- Call `codemap_current_behavior` and `codemap_check_terms` before writing or reviewing a requirement.
- Cite every claim about current behaviour ([N…], [E…], [D…], RULE-…, file:line) or mark it as an assumption.
- Only approved rules are facts; drafts, proposals and NEEDS REVIEW items are not.
- Turn gaps and contradictions into closed questions (`codemap_propose_question`); never decide business rules.
- Keep to the approved impact.yaml; always include a "Not certain" section.
