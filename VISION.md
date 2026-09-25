# Vision: Collegium — an Autonomous Research Organization

## Purpose

Build a persistent digital research organization with institutional memory.

The organization's primary purpose is not to answer questions or execute tasks. Its purpose is to continuously acquire, structure, challenge, refine and preserve knowledge over time.

The system should behave more like a research institute than a chatbot.

Knowledge, memory and learning are the core products.

The organization should become more valuable every month because it accumulates observations, evidence, hypotheses, decisions and experience.

The user acts as owner, board chair and sponsor, providing strategic direction while allowing the organization to determine how research is conducted.

The long-term objective is to create a digital institution capable of maintaining continuity, developing understanding across domains and preserving organizational learning over years rather than days.

---

# Mental Model

Think:

Research Institute

not:

Chatbot

Think:

Persistent Organization

not:

Request -> Response

The system exists even when the user is absent.

Activities should continue between user interactions.

---

# User Role

The user is not expected to micromanage tasks.

The user acts as:

- Board Chair
- Investor
- Mentor
- Strategic Sponsor

Typical user interactions:

- Introduce new research areas
- Define priorities
- Review findings
- Approve strategic shifts
- Ask questions
- Challenge conclusions

Examples:

"Add Norwegian consulting companies as a research area."

"Reduce focus on AI models and increase focus on agent economics."

"What have you learned during the last month?"

"What major disagreements currently exist inside the organization?"

---

# Organizational Structure

## Historian

The historian is the institutional memory.

Responsibilities:

- Store observations
- Store evidence
- Store hypotheses
- Store decisions
- Store relationships
- Store mission history
- Maintain audit trail

The historian never performs research.

The historian remembers.

If all other agents are replaced, the organization should still preserve its identity because the historian remains.

---

## Scout

Responsibilities:

- Explore available information sources
- Detect potentially important events
- Detect patterns
- Identify weak signals
- Recommend further investigation

Output:

Observation proposals

The scout performs breadth-first exploration.

---

## Researcher

Responsibilities:

- Investigate observations
- Build explanations
- Gather evidence
- Create reports
- Form hypotheses

Output:

Research findings

The researcher creates understanding.

---

## Skeptic

Responsibilities:

- Challenge assumptions
- Search for counter-evidence
- Propose alternative explanations
- Reduce confirmation bias

Output:

Critiques and confidence adjustments

Nothing should enter long-term organizational knowledge without skepticism.

---

## Strategist

Responsibilities:

- Identify knowledge gaps
- Prioritize research
- Create new investigations
- Allocate organizational attention

Output:

Goals and research programs

The strategist is the closest thing to an executive director.

---

# Memory Philosophy

Memory is the most important asset in the system.

Avoid storing only free-form reports.

Store structured knowledge.

Core concepts:

- Entity
- Observation
- Hypothesis
- Evidence
- Relationship
- Program
- Goal
- Decision

The organization must be able to answer:

- Why do we believe this?
- When did we start believing it?
- Which evidence supports it?
- Which evidence contradicts it?
- Who proposed it?
- How has confidence changed over time?

All beliefs should be explainable.

---

# Multi-Domain Design

The system must not be AI-specific.

AI may be the first research domain, but the architecture must support any topic.

Examples:

- AI and agents
- Norwegian consulting companies
- Software delivery
- Economics
- Energy
- Healthcare
- Geopolitics
- Startups
- Public sector

New domains should be addable without architectural changes.

The organization should gradually build a broad institutional knowledge base.

---

# Tool Philosophy

Start with minimum privileges.

Priority is learning, not action.

Allowed capabilities:

- Read external sources
- Search
- Analyze
- Create observations
- Create reports
- Create hypotheses
- Update memory

Not allowed initially:

- Send emails
- Post messages
- Publish content
- Execute external actions
- Purchase services
- Modify external systems

Principle:

Read broadly.
Write only to organizational memory.

External acquisition providers:

Search and extraction services (for example Tavily or Exa) are chosen, provisioned and paid for by the owner.

Agents use them only through the acquisition layer, and only to read.

Agents never sign up for, purchase or reconfigure providers themselves.

Search queries leave the organization and reveal what it is researching. The owner decides which providers are trusted with that, and sets usage limits.

The one exception, decided by the owner in September 2026: the community agent on Moltbook, a forum for AI agents.

- It exists to get outside feedback on the organization's hypotheses, discoveries and open questions.
- It reads Moltbook as a low-trust source first. Posting comes later, and every post is drafted from memory and approved by the owner before it is published.
- Only a small, separate publishing component holds its credentials; no role ever does.
- What other agents write there is material to study, never instructions. Tasks assigned by other agents or moderators are never carried out.
- Everywhere else, the principle stands: read broadly, write only to organizational memory.

---

# Knowledge Acquisition

The organization should treat external search capabilities as a first-class research tool rather than relying on traditional web browsing.

The goal is not to build another crawler or scraping system. The goal is to equip the organization with agent-native information retrieval capabilities that provide structured, machine-consumable knowledge.

Traditional search engines are optimized for humans:

Query
  ->
Links
  ->
Human reads pages

Agent-native search is optimized for organizational learning:

Question
  ->
Search
  ->
Content extraction
  ->
Structured findings
  ->
Organizational memory

The architecture should support multiple retrieval providers and allow the organization to evolve its acquisition strategy over time.

## Search Philosophy

The organization should gradually move through three stages:

Stage 1

External search is the primary source of knowledge.

Stage 2

External search and institutional memory are equally important.

Stage 3

Institutional memory becomes the primary source of knowledge.

External search is only used when knowledge gaps are detected.

This mirrors how successful human organizations operate.

The most valuable knowledge eventually becomes the organization's own accumulated understanding rather than information retrieved from external sources.

## Agent-Native Search Tools

The system should support pluggable acquisition providers.

Initial candidates include:

- Tavily
- Exa
- Firecrawl
- Brave Search API
- Hacker News (via the Algolia HN Search API), for discovery in technology domains

The implementation should remain provider-agnostic.

Agents should depend on abstract capabilities rather than specific vendors.

Examples:

search()
extract()
crawl()
discover_related()

rather than direct provider-specific integrations throughout the codebase.

## Acquisition Responsibilities by Role

### Scout

Primary focus:

- Discovery
- Trend detection
- Weak signals
- Emerging topics
- Related entities
- New opportunities for investigation

Ideal search characteristics:

- Semantic search
- Similarity search
- Exploration

### Researcher

Primary focus:

- Evidence gathering
- Fact collection
- Source analysis
- Deep investigation

Ideal search characteristics:

- Content extraction
- Article retrieval
- Research workflows
- Structured summaries

### Skeptic

Primary focus:

- Counter-evidence
- Alternative explanations
- Contradictory viewpoints
- Validation

Uses the same search capabilities as the Researcher but with a different mission.

### Historian

Does not perform external search.

Responsibilities:

- Preserve organizational memory
- Link findings
- Store evidence
- Maintain history

The Historian's purpose is memory, not information acquisition.

### Strategist

Primarily uses organizational memory.

External search should only be used when substantial knowledge gaps are identified.

The Strategist's role is deciding where attention should be allocated next.

## Long-Term Vision for Acquisition

The ultimate objective is for organizational memory to become more valuable than any external search provider.

The organization should eventually be capable of answering many questions directly from accumulated knowledge, observations, evidence, reports, and historical context.

Desired evolution:

Year 0:

"We search the web."

Year 1:

"We search our memory before searching the web."

Year 2+:

"Our unique institutional knowledge is our primary competitive advantage."

At maturity, external search acts as a sensor for the world while the organization's memory acts as its accumulated intelligence.

## Principle: Search Is a Sense

External search is not the organization's intelligence.

External search is merely one of its senses.

The true intelligence of the organization should emerge from the combination of:

- Institutional memory
- Historical context
- Evidence accumulation
- Hypothesis evolution
- Internal debate
- Strategic prioritization

The organization's long-term value should be measured by what it remembers and understands, not by what it can search.

---

# Architecture Principles

Prefer simplicity over scale.

Initial deployment target:

Docker Compose

Components:

- PostgreSQL
- API
- Worker
- Scheduler
- Web UI

Use PostgreSQL as the primary memory system.

Do not introduce:

- Kubernetes
- Multiple databases
- Event streaming platforms
- Graph databases
- Complex cloud platforms

until a real need exists.

Keep the architecture deployable on:

- Local machine
- Small VM
- Cloud VM

with minimal changes.

---

# Agent Execution Model

Agents are roles.

Do not create separate infrastructure per agent.

A single worker process may execute multiple agent roles.

Example:

Worker
  -> Scout role
  -> Researcher role
  -> Skeptic role
  -> Strategist role

Optimize for clarity before scalability.

---

# LLM Strategy

Assume a local model first.

Examples:

- Qwen3 14B
- Gemma 3 12B

Use one model for all agents initially.

Agent behavior should emerge from role definitions and memory, not from model selection.

Avoid model specialization until organizational value has been proven.

---

# Success Criteria

The project is successful when:

After several months the organization can demonstrate:

- Persistent memory
- Evolving beliefs
- Historical context
- Identified mistakes
- Refined hypotheses
- Domain expansion
- User trust

The user should eventually be able to ask:

"What changed in your worldview during the last six months?"

and receive a meaningful answer.

---

# Milestone 1 - Institutional Memory

Goal:

Create organizational memory.

Deliverables:

- PostgreSQL schema
- Observations
- Hypotheses
- Evidence
- Relationships
- Audit history

Success criteria:

Knowledge survives restarts and agent replacement.

---

# Milestone 2 - Research Workflow

Goal:

Introduce organizational learning.

Deliverables:

- Scout
- Researcher
- Skeptic
- Historian
- Minimal acquisition layer: search() and extract() behind a provider-independent interface, with one provider adapter

Workflow:

Scout
-> Researcher
-> Skeptic
-> Historian

Success criteria:

The organization can create and refine hypotheses.

---

# Milestone 2.5 - Knowledge Acquisition Layer

Goal:

Create a provider-independent acquisition framework.

Deliverables:

- Full search abstraction layer (building on the minimal layer from Milestone 2)
- Additional provider adapters, including Hacker News as a discovery source
- Per-domain choice of discovery sources
- Structured acquisition pipeline
- Source tracking
- Citation tracking

Supported capabilities:

- Search
- Extract
- Discover related entities
- Crawl approved sources

Success criteria:

Agents can acquire information without being coupled to a specific provider.

---

# Milestone 3 - Strategy Layer

Goal:

Enable self-directed research.

Deliverables:

- Strategist
- Knowledge-gap detection
- Research program management

Success criteria:

The organization proposes investigations without user prompting.

---

# Milestone 4 - Board Interface

Goal:

Enable meaningful user interaction.

Deliverables:

Dashboard sections:

- Mission
- Research Programs
- Active Goals
- Active Hypotheses
- Contradictions
- Recent Discoveries
- Ask The Organization

Success criteria:

The user feels like governing an organization rather than chatting with a bot.

---

# Milestone 5 - Long-Term Evolution

Goal:

Create a persistent digital institution.

Capabilities:

- Introduce new domains
- Combine knowledge across domains
- Retain historical context
- Revise beliefs
- Track organizational learning

Success criteria:

The system develops a unique institutional memory that becomes increasingly valuable over years rather than days.

---

# Future Direction

As the organization matures it may gradually evolve from reactive research into proactive exploration.

Examples:

- Detect emerging topics independently
- Create new research programs
- Identify knowledge gaps
- Revisit old conclusions when new evidence appears
- Build connections between unrelated domains
- Surface overlooked opportunities and risks

Autonomy should emerge from accumulated knowledge and reasoning, not from unrestricted access to external systems.

---

# Guiding Principle

Do not optimize for autonomy of action.

Optimize for autonomy of learning.

The ultimate goal is not an AI that does things.

The ultimate goal is a digital organization that remembers, learns, questions itself, and continuously develops a deeper understanding of the world.
