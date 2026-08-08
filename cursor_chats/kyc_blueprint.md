# Build a Tavily-powered KYC Investigation Agent

Build a production-inspired, runnable KYC Investigation Agent that demonstrates how Tavily can power the **external investigation stage** of an enterprise Know Your Customer (KYC) workflow.

The goal is **not** to build a chatbot or generic search agent.

The goal is to demonstrate how adaptive search, structured evidence gathering, iterative retrieval, and transparent citations can reduce the manual effort required by compliance analysts performing customer due diligence.

The implementation should emphasize Tavily's strengths:

- adaptive search
- iterative investigations
- evidence-backed reasoning
- high quality retrieval
- source transparency
- traceability

The result should feel like a realistic internal compliance tool rather than an AI demo.

Always show the implementation plan before editing files, but do not wait for approval.

---

# Project

This is a new repository.

Implement a lightweight Python CLI using LangGraph (preferred over a single LangChain agent) that walks through a realistic KYC investigation.

The workflow should begin with KYC onboarding documents (or representative sample documents) rather than asking only for a company name.

Example inputs:

- Certificate of Incorporation
- Trade Licence
- Passport
- National ID
- Shareholder Register
- Ultimate Beneficial Owner declaration

These can be mocked/sample documents.

The system should extract entities from the uploaded documents before beginning external investigation.

---

# Customer

Corporate real estate company leasing office space to businesses.

This reflects a real enterprise workflow.

The onboarding team already performs:

- document collection
- adverse media checks
- beneficial ownership verification
- WorldCheck / sanctions screening
- analyst review

This project improves the **manual web investigation** portion using Tavily.

Do not attempt to replace sanctions providers or identity verification vendors.

---

# Business Problem

Reduce the time required for external KYC investigations while improving evidence quality, source traceability, and analyst confidence.

The output should help analysts decide:

- Proceed
- Review
- Escalate for Enhanced Due Diligence

It should **never** automatically approve or reject a customer.

---

# High-level Workflow

```
Upload KYC Documents
        │
        ▼
Entity Extraction
        │
        ▼
Entity Resolution
        │
        ▼
Investigation Planner
        │
 ┌──────┼─────────────┐
 ▼      ▼             ▼
Company UBO      Adverse Media
Research Research Investigation
        │
        ▼
Structured Evidence Store
        │
        ▼
Coverage Evaluation
        │
Missing evidence?
 │             │
Yes            No
 │             ▼
Generate     Risk Assessment
More Searches
 │             │
 └─────────────┘
        ▼
Analyst Investigation Report
```

The implementation should use LangGraph because each stage represents a distinct workflow node.

---

# Tavily's Role

Tavily powers **all external research**, including:

- company research
- adverse media
- management investigations
- litigation
- regulatory issues
- reputation
- ownership corroboration
- UBO investigation

Do **not** use Tavily for OCR or document parsing.

Instead:

Documents
↓

Extract entities

↓

Investigate entities with Tavily

---

# Search Strategy

Avoid generic searches.

Instead, generate focused investigation searches.

Examples:

```
Acme Holdings ownership

Acme Holdings litigation

Acme Holdings sanctions

Acme Holdings fraud

Acme Holdings regulatory action

Acme Holdings money laundering

Acme Holdings executive interview

John Smith fraud

John Smith sanctions

John Smith previous companies
```

Searches should be deterministic and driven by investigation objectives.

The planner should know **what** to investigate before searching.

---

# Adaptive Retrieval

Search is an iterative investigation process.

The workflow should be:

```
Search

↓

Extract evidence

↓

Evaluate coverage

↓

Generate follow-up searches

↓

Search again

↓

Repeat until sufficient evidence exists
```

The implementation should prioritize:

- adaptive retrieval
- follow-up searches
- query refinement
- investigation expansion

over simply calling Tavily once.

---

# Parallelism

Independent investigations should execute concurrently where possible.

Examples:

- Company investigation
- UBO investigation
- Executive investigation
- Adverse media
- Reputation
- Litigation

This demonstrates production thinking around latency.

---

# Evidence-first Architecture

Do **not** summarize immediately.

Search results become structured evidence.

Suggested schema:

```python
Evidence(
    entity,
    category,
    claim,
    confidence,
    source,
    published_date,
    url
)
```

Everything downstream should consume evidence objects rather than raw Tavily responses.

---

# Entity Resolution

Implement lightweight LLM-assisted entity resolution.

Example:

```
ABC Holdings Ltd

ABC Holdings Limited

ABC Holdings
```

should be normalized before investigation.

This improves retrieval quality.

---

# UBO Strategy

If a UBO is present in the uploaded documents:

- investigate them

If absent:

- attempt to infer likely ownership from public information
- clearly communicate uncertainty
- never present inferred ownership as verified fact

---

# Reflection Loop

Reflection should improve investigations rather than critique answers.

Reflection asks:

- Which investigation objectives lack evidence?
- Which findings rely on too few sources?
- Which entities need further investigation?

The output of reflection should be:

new Tavily searches

—not simply another LLM response.

---

# Report

Generate a structured investigation report.

Sections might include:

- Executive Summary
- Company Overview
- Management Investigation
- Ultimate Beneficial Ownership
- Adverse Media
- Litigation
- Reputation
- Risk Findings
- Recommendation

Recommendation must be one of:

- Proceed
- Review
- Escalate

---

# Every Finding Must Include

- finding
- confidence
- supporting sources
- publication dates
- URLs
- evidence count

Example:

```
Finding

Previous litigation involving subsidiary.

Confidence

High

Evidence

3 independent sources

Sources

...

Recommendation

Review
```

---

# Investigation Coverage

Include an investigation coverage section.

Example:

```
✓ Company profile

✓ Executive background

✓ Adverse media

✓ Litigation

✓ Reputation

✓ UBO investigation

⚠ Ownership verification incomplete

Coverage

86%
```

The agent should explain:

- what was investigated
- what was found
- what could not be verified
- suggested next manual actions

Example:

Missing:

Official shareholder register.

Suggested next step:

Request shareholder register from applicant.

---

# Visible Investigation Timeline

Display the investigation process.

Example:

```
09:41

Extracted directors

09:42

Searching company

09:42

Searching UBO

09:42

Searching litigation

09:43

Coverage insufficient

09:43

Launching follow-up searches

09:44

Generating report
```

This replaces heavy observability frameworks.

---

# Tavily Workflow Visibility

Make Tavily's lifecycle transparent.

Display:

- generated search queries
- investigation objective
- Tavily parameters
- timestamps
- response duration
- retrieved sources
- extracted evidence
- reflection decisions
- follow-up searches
- final evidence selected

Never expose credentials.

---

# Context Engineering

Reuse evidence already collected.

Example:

If company research identifies the CEO,

the UBO investigation should reuse that information rather than searching again unnecessarily.

Avoid redundant searches whenever possible.

---

# Technical Focus

The project should demonstrate:

- LangGraph workflow orchestration
- adaptive retrieval
- iterative investigation
- structured evidence
- entity resolution
- evidence aggregation
- reflection-driven search
- context reuse
- transparent citations

Avoid unnecessary complexity.

Do not build:

- authentication
- databases
- production deployments
- notification systems
- background workers
- dashboards

The emphasis is demonstrating an enterprise-grade investigation workflow.

---

# Tavily Setup

Install and use Tavily Skills.

Reuse any existing Tavily SDK if present.

Otherwise use the Python SDK.

Use:

- `include_answer="advanced"`
- focused search queries
- selective extraction only when necessary
- deduplicate evidence
- preserve source URLs and publication dates

Never expose API keys.

---

# Success Criteria

A reviewer should finish the demo believing:

> "This isn't a chatbot using Tavily.

> This is an adaptive investigation engine that treats search as an iterative enterprise workflow."

The primary innovation is **adaptive retrieval**:

Rather than treating Tavily as a single search call, the system continuously plans, searches, evaluates evidence, generates new search objectives, and expands the investigation until sufficient evidence exists to support a recommendation.