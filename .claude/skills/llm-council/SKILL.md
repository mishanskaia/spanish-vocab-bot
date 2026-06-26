---
name: llm-council

description: "Run any question, idea, or decision through a council of 5 AI advisors who independently analyze it, peer-review each other anonymously, and synthesize a final verdict. Based on Karpathy's LLM Council methodology. MANDATORY TRIGGERS: 'council this', 'run the council', 'war room this', 'pressure-test this', 'stress-test this', 'debate this'. STRONG TRIGGERS (use when combined with a real decision or tradeoff): 'should I X or Y', 'which option', 'what would you do', 'is this the right move', 'validate this', 'get multiple perspectives', 'I can't decide', 'I'm torn between'. Do NOT trigger on simple yes/no questions, factual lookups, or casual 'should I' without a meaningful tradeoff (e.g. 'should I use markdown' is not a council question). DO trigger when the user presents a genuine decision with stakes, multiple options, and context that suggests they want it pressure-tested from multiple angles."

---

# LLM Council

You ask one AI a question, you get one answer. That answer might be great. It might be mid. You have no way to tell because you only saw one perspective.

The council fixes this. It runs your question through 5 independent advisors, each thinking from a fundamentally different angle. Then they review each other's work. Then a chairman synthesizes everything into a final recommendation that tells you where the advisors agree, where they clash, and what you should actually do.

This is adapted from Andrej Karpathy's LLM Council. He dispatches queries to multiple models, has them peer-review each other anonymously, then a chairman produces the final answer. We do the same thing inside Claude using sub-agents (or, if sub-agents aren't available in this environment, by reasoning through each advisor role sequentially in a single response) with different thinking lenses instead of different models.

---
## language

CRITICAL: respond in the same language the user used in their trigger message. If they wrote in Russian, the entire output must be in Russian: the framed question, all 5 advisor responses, all peer reviews, and the final verdict — including translated section headers and advisor names (see below).

This instruction must be repeated explicitly inside every individual prompt you construct for an advisor, reviewer, or chairman sub-agent — sub-agents start with a fresh context and do not automatically see this skill file, so each delegated prompt needs its own line like: "Respond entirely in Russian." If you are reasoning through the advisor roles sequentially yourself (no sub-agents available), this is automatic since you keep the full context — but stay vigilant, don't slip into English partway through.

Russian section headers:
- "Council Verdict" → "Вердикт совета"
- "Where the Council Agrees" → "В чём совет согласен"
- "Where the Council Clashes" → "В чём совет расходится"
- "Blind Spots the Council Caught" → "Слепые зоны, которые заметил совет"
- "The Recommendation" → "Рекомендация"
- "The One Thing to Do First" → "Первый шаг"

Russian advisor names:
- The Contrarian → Скептик
- The First Principles Thinker → Мыслитель первых принципов
- The Expansionist → Визионер
- The Outsider → Посторонний
- The Executor → Исполнитель

---

---

## when to run the council

The council is for questions where being wrong is expensive.

Good council questions:
- "Should I launch a $97 workshop or a $497 course?"
- "Which of these 3 positioning angles is strongest?"
- "I'm thinking of pivoting from X to Y. Am I crazy?"
- "Here's my landing page copy. What's weak?"
- "Should I hire a VA or build an automation first?"

Bad council questions:
- "What's the capital of France?" (one right answer, no need for perspectives)
- "Write me a tweet" (creation task, not a decision)
- "Summarize this article" (processing task, not judgment)

The council shines when there's genuine uncertainty and the cost of a bad call is high. If you already know the answer and just want validation, the council will likely tell you things you don't want to hear. That's the point.

---

## the five advisors

Each advisor thinks from a different angle. They're not job titles or personas. They're thinking styles that naturally create tension with each other.

### 1. The Contrarian
Actively looks for what's wrong, what's missing, what will fail. Assumes the idea has a fatal flaw and tries to find it. If everything looks solid, digs deeper. The Contrarian is not a pessimist. They're the friend who saves you from a bad deal by asking the questions you're avoiding.

### 2. The First Principles Thinker
Ignores the surface-level question and asks "what are we actually trying to solve here?" Strips away assumptions. Rebuilds the problem from the ground up. Sometimes the most valuable council output is the First Principles Thinker saying "you're asking the wrong question entirely."

### 3. The Expansionist
Looks for upside everyone else is missing. What could be bigger? What adjacent opportunity is hiding? What's being undervalued? The Expansionist doesn't care about risk (that's the Contrarian's job). They care about what happens if this works even better than expected.

### 4. The Outsider
Has zero context about you, your field, or your history. Responds purely to what's in front of them. This is the most underrated advisor. Experts develop blind spots. The Outsider catches the curse of knowledge: things that are obvious to you but confusing to everyone else.

### 5. The Executor
Only cares about one thing: can this actually be done, and what's the fastest path to doing it? Ignores theory, strategy, and big-picture thinking. The Executor looks at every idea through the lens of "OK but what do you do Monday morning?" If an idea sounds brilliant but has no clear first step, the Executor will say so.

**Why these five:** They create three natural tensions. Contrarian vs Expansionist (downside vs upside). First Principles vs Executor (rethink everything vs just do it). The Outsider sits in the middle keeping everyone honest by seeing what fresh eyes see.

---

## how a council session works

### step 1: frame the question (with context enrichment)

When the user says "council this" (or any trigger phrase), do two things before framing:

**A. Scan for context.** The user's question is often just the tip of the iceberg. If working inside a project/workspace with files (e.g. Claude Code, or a Claude.ai Project with documents attached), quickly check for relevant context: project instructions, notes, prior related discussion, or any files the user explicitly referenced or attached. Don't spend more than a minute on this — you're looking for the 2-3 pieces of context that would let advisors give specific, grounded advice instead of generic takes. If no such context exists (e.g. a plain chat), just use what the user has said in the conversation.

**B. Frame the question.** Take the user's raw question AND any enriched context and
1. The core decision or question
2. Key context from the user's message
3. Key context from available files/history (stage, constraints, past results, relevant numbers)
4. What's at stake (why this decision matters)

Don't add your own opinion. Don't steer it. But DO make sure each advisor has enough context to give a specific, grounded answer rather than generic advice.

If the question is too vague ("council this: my business"), ask one clarifying question. Just one. Then proceed.

### step 2: convene the council

If sub-agents are available, spawn all 5 advisors simultaneously as sub-agents. If not, reason through each advisor's perspective yourself, one at a time, in full — writing as if each were an independent voice, not blending them together. Each gets:
1. Their advisor identity and thinking style (from the descriptions above)
2. The framed question
3. A clear instruction: respond independently. Do not hedge. Do not try to be balanced. Lean fully into your assigned perspective. If you see a fatal flaw, say it. If you see massive upside, say it. Your job is to represent your angle as strongly as possible. The synthesis comes later.

Each advisor's response should be 150-300 words. Long enough to be substantive, short enough to be scannable.

### step 3: peer review

Collect all 5 advisor responses. Anonymize them as Response A through E (randomize which advisor maps to which letter so there's no positional bias).

For each of the 5 advisors, generate a peer review that sees all 5 anonymized responses and answers three questions:
1. Which response is the strongest and why? (pick one)
2. Which response has the biggest blind spot and what is it?
3. What did ALL responses miss that the council should consider?

Keep each review under 200 words. Be direct.

### step 4: chairman synthesis

One final pass synthesizes everything: the original question, all 5 advisor responses (de-anonymized), and all 5 peer reviews.

The chairman's job is to produce the final council output:

1. **Where the council agrees** — points multiple advisors converged on independently. High-confidence signals.
2. **Where the council clashes** — genuine disagreements. Don't smooth these over. Present both sides and explain why reasonable advisors disagree.
3. **Blind spots the council caught** — things that only emerged through peer review.
4. **The recommendation** — a clear, actionable recommendation. Not "it depends." The chairman can disagree with the majority if the reasoning supports it.
5. **The one thing you should do first** — a single concrete next step. Not a list. One thing.

### step 5: present the verdict in chat

Present the full verdict directly in the conversation using markdown. Do NOT generate an HTML report or any files unless the user asks for one.

Format the output as:
Council Verdict: {short topic}

Where the Council Agrees

{content}

Where the Council Clashes

{content}

Blind Spots the Council Caught

{content}

The Recommendation

{content}

The One Thing to Do First

{content}

Keep it scannable. Use bullet points where helpful.

### step 6: save the transcript (optional)

Only save/export a transcript if the user asks for it or the question is significant enough to reference later.

---

## important notes

- **Always anonymize for peer review.** If reviewers know which advisor said what, they'll defer to certain thinking styles instead of evaluating on merit.
- **The chairman can disagree with the majority.** If 4 out of 5 advisors say "do it" but the reasoning of the 1 dissenter is strongest, the chairman should side with the dissenter and explain why.
- **Don't council trivial questions.** If the user asks something with one right answer, just answer it. The council is for genuine uncertainty where multiple perspectives add value.
- **Be direct, not diplomatic.** The whole point of the council is to give the user clarity they couldn't get from a single perspective — don't let the synthesis collapse back into "it depends, consider both sides."

---

*Adapted from Andrej Karpathy's LLM Council methodology. Original Claude skill packaging by Ole Lehmann.*

