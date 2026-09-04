# Project Specification: Multi-Agent Wealth Advisory RAG Pipeline (v2)

## 1. Objective
Upgrade the existing financial research agent to a **Hierarchical Multi-Agent Architecture** (Supervisor pattern). The system will feature vector-based search caching, structured source citations, execution observability, and a dedicated Compliance Critic powered by Groq's `meta-llama/llama-prompt-guard-2-86m` model.

## 2. Technology Stack
- **Language:** Python 3.11+
- **Agent Orchestration:** `langgraph` (Supervisor routing graph)
- **Primary LLM (Synthesis/Routing):** `google-genai` (Gemini 2.5 Flash)
- **Compliance Critic LLM:** `groq` (Model: `meta-llama/llama-prompt-guard-2-86m`)
- **Web Search Tool:** `tavily-python`
- **Database & Caching:** Neon PostgreSQL with `pgvector` extension (using Prisma Client Python)
- **Observability:** `langsmith` (via `LANGCHAIN_TRACING_V2=true`)
- **Job Scheduling:** `inngest`

## 3. Multi-Agent Architecture & LangGraph Nodes
The agent workflow must be implemented as a cyclic state machine where a `Supervisor` routes tasks to specialized worker nodes. 

**Graph Nodes:**
1. **`supervisor_node` (Gemini):** Evaluates current state and routes to `researcher`, `writer`, `compliance_critic`, or `FINISH`.
2. **`researcher_node` (Gemini + Tavily + pgvector):** 
   - **Vector Caching Logic:** Before querying Tavily, convert the search query to an embedding and check the Neon `pgvector` cache. If a highly similar query was executed in the last 4 hours, use the cached JSON results.
   - If no cache exists, call Tavily, append the JSON results, and save the new context to the Neon vector cache.
3. **`writer_node` (Gemini):** Drafts the briefing with a Macro View and Portfolio Impact section.
4. **`compliance_critic_node` (Groq):** 
   - Must use the Groq Python SDK and specifically the `meta-llama/llama-prompt-guard-2-86m` model. 
   - Evaluates the draft for SEC/FINRA compliance (no directive buy/sell language, all claims grounded in sources). 
   - Outputs a strict JSON decision: `{"is_compliant": boolean, "critique_reason": string}`.
   - If `is_compliant` is false, append the critique to state and the Supervisor will route it back to the `writer_node`.

## 4. Implementation Steps for the Agent
Agent, please execute the following steps sequentially. Generate an implementation plan artifact before writing code.

- **Step 1:** Update `requirements.txt` to include `groq`, `langsmith`, and vector embedding libraries.
- **Step 2:** Update the `schema.prisma` file to support:
  - The `pgvector` PostgreSQL extension.
  - A `SearchCache` model containing the query embedding, JSON results, and a timestamp.
  - A `Citation` model linked to the `Briefing` model to store URLs and publication dates from Tavily.
- **Step 3:** Implement the Multi-Agent LangGraph logic in `agent.py` as defined in Section 3. Ensure the `compliance_critic_node` strictly uses the Groq client.
- **Step 4:** Integrate LangSmith by ensuring all LLM and graph executions are properly traced (use environment variables `LANGCHAIN_API_KEY` and `LANGCHAIN_PROJECT`).

## 5. Strict Execution Rules & Compliance Guardrails
- **Groq Model Requirement:** The `compliance_critic_node` MUST ONLY use `meta-llama/llama-prompt-guard-2-86m`. Do not use Gemini or Ollama for this specific node.
- **Structured Citations:** The final output saved to the database must include the array of URLs used to generate the briefing.
- **Local Testing:** Provide a local execution block (`if __name__ == "__main__":`) to run the supervisor loop in the terminal so I can verify the agent handoffs and LangSmith traces before scheduling with Inngest.