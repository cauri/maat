# Prompt template (cauri's standard)

**Every in-platform agent prompt in Maat follows this structure** — the runtime prompts in
`python/maat/prompts.py` (extract, classify, extremity, acquire_queries, …), the console
assistant, and any future LLM-facing text the app runs. Optional sections may be dropped when not
relevant; the named sections that are present should keep these names and this order.

> Prompt **content** is still co-designed with cauri — this template is the *shape*, not a licence
> to write or change runtime prompts unilaterally (see `CLAUDE.md`). Put runtime variables (`{var}`)
> under **CONTEXT**.

```
ROLE
You are an [domain] specialist. Your role is to [core function].

USER ROLE (optional)
I am a [audience role]. Your responsibility is to [what I bring].

GOALS
- [Concrete outcome 1]
- [Concrete outcome 2]
- [Concrete outcome 3]

PROCESS (either this)
1. [First ordered step].
2. [Second ordered step].
3. [Third ordered step].

INSTRUCTIONS (or this)
- [Unordered directive 1]
- [Unordered directive 2]
- [Unordered directive 3]

GUIDELINES
- If [condition], [action]
- Prefer [X] over [Y]
- Do not [edge case behaviour]

GUARDRAILS (optional)
- Do not [hard constraint]
- Never [security or hallucination prevention]
- Validate [inputs] against [known definitions]

TOOLS (optional)
You can call [tool] using [invocation method].
{available_tools}

TONE (optional)
- [Register: professional, conversational, etc.]
- [Vocabulary level]
- [Formatting expectations]

OUTPUT FORMAT (optional)
- [Structure: markdown, JSON, narrative, etc.]
- [Required components: citations, headers, etc.]

CONTEXT
[VARIABLE NAME]
{runtime_variable}

[VARIABLE NAME]
{runtime_variable}

EXAMPLES (optional)
User: [Example input]
Agent: [Example acknowledgement]
Output: {example_output}
```
