---
kind: spec
gate: human                  # human | auto | auto_answer | none
view: [spec/*.md]            # extra files shown in the step's Files tab
pass: ["exists spec/spec.md"]   # exists <glob> · contains <file> <regex> · run <cmd>; the step fails unless all hold
---
Notes: this text is added to every LLM task of the step.
Keep the specification to one page; every requirement gets an id.
