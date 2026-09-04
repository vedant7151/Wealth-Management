# Project Specification: Wealth Advisory Research Digest Agent

## 1. Objective
Build an autonomous Retrieval-Augmented Generation (RAG) agent that runs on a schedule. The agent must fetch client portfolio themes from a database, search the web for relevant real-time financial news, evaluate the quality of the news, and draft an SEC-compliant morning briefing. 

## 2. Technology Stack
- **Language:** Python 3.11+
- **Agent Orchestration:** `langgraph` (StateGraph workflow)
- **LLM/Reasoning:** `google-genai` (Gemini 2.5 Flash)
- **Web Search Tool:** `tavily-python` (Tavily Search API)
- **Database/ORM:** Neon PostgreSQL (using Prisma Client Python)
- **Job Scheduling:** `inngest` (Python SDK for automated CRON execution)
- **Environment:** `python-dotenv` for managing API keys (GEMINI_API_KEY, TAVILY_API_KEY, DATABASE_URL).

## 3. Architecture & LangGraph Nodes
The agent workflow must be implemented as a cyclic state machine using LangGraph. The `GraphState` dictionary should track: `portfolio_themes`, `queries_to_run`, `gathered_news`, `iteration_count`, and `final_briefing`.

**Graph Nodes:**
1. **`plan_search`**: LLM takes `portfolio_themes` and generates 2-3 highly targeted search queries. Outputs a JSON array of strings.
2. **`execute_search`**: Iterates through queries, calls the Tavily API, and appends the JSON text results to `gathered_news`.
3. **`evaluate_and_route` (Conditional Edge)**: LLM reviews `gathered_news`. If the data lacks sufficient financial depth for the given themes, return "search" to route back to `plan_search`. If the data is good (or `iteration_count` >= 2), return "draft" to route forward.
4. **`draft_briefing`**: Final synthesis node. Formats the data into a Markdown briefing with a Macro View and Portfolio Impact section.

## 4. Implementation Steps for the Agent
Agent, please execute the following steps sequentially. Generate an implementation plan artifact before writing code.

- **Step 1:** Initialize the Python project, set up the virtual environment, and install all required dependencies in `requirements.txt`.
- **Step 2:** Scaffold the Prisma schema for a `ClientProfile` table containing `id`, `name`, and `portfolio_themes` (String array). Generate the Python Prisma client.
- **Step 3:** Implement the core LangGraph logic in `agent.py` as defined in Section 3. Use `google-genai` for all reasoning and text generation tasks.
- **Step 4:** Wrap the LangGraph invocation inside an Inngest function (`inngest_client.create_function`) configured with a CRON trigger (e.g., `"0 6 * * 1-5"` for 6 AM on weekdays).

## 5. Strict Execution Rules & Compliance Guardrails
- **No Hallucinations:** The `draft_briefing` node must ONLY use facts present in the `gathered_news` string.
- **SEC Compliance Guardrail:** In the system instructions for the `draft_briefing` LLM call, you MUST include the following text verbatim: *"CRITICAL: Never make definitive buy, sell, or hold recommendations. Phrase all impacts strictly as potential risks or potential tailwinds based on historical correlations."*
- **Local Testing:** Provide a local execution block (`if __name__ == "__main__":`) that mocks the database call so I can test the LangGraph loop in the terminal before deploying the Inngest CRON job.