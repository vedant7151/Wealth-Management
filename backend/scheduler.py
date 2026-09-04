import inngest
from agent import app
from prisma import Prisma

# Initialize the Inngest client
inngest_client = inngest.Inngest(app_id="wealth-agent")

@inngest_client.create_function(
    fn_id="morning-briefing-job",
    name="Generate Morning Briefing",
    trigger=inngest.TriggerCron(cron="0 6 * * 1-5"),
)
async def generate_morning_briefing(ctx: inngest.Context, step: inngest.Step) -> dict:
    
    # Step 1: Fetch themes from Prisma DB synchronously inside a step wrapper
    def fetch_themes():
        db = Prisma()
        db.connect()
        profiles = db.clientprofile.find_many()
        db.disconnect()
        
        # Flatten all themes for the briefing agent, or collect from a specific user
        all_themes = []
        for p in profiles:
            all_themes.extend(p.portfolio_themes)
            
        return all_themes
        
    themes = await step.run("fetch_portfolio_themes", fetch_themes)
    
    # Step 2: Run the LangGraph agent
    def run_agent():
        mock_input = {
            "portfolio_themes": themes,
            "queries_to_run": [],
            "gathered_news": "",
            "iteration_count": 0,
            "final_briefing": ""
        }
        
        final_state = None
        for output in app.stream(mock_input):
            for key, value in output.items():
                print(f"Finished node: {key}")
                final_state = value
        
        return final_state.get("final_briefing", "Failed to generate briefing.")
        
    briefing = await step.run("run_langgraph_agent", run_agent)
    
    # Normally, you would dispatch an email or save to DB here.
    return {"status": "success", "briefing_length": len(briefing)}

# Create an ASGI app to serve the Inngest endpoints (e.g. FastAPI / Flask / Tornado etc.)
# import fastapi
# from inngest.fast_api import serve
# api = fastapi.FastAPI()
# serve(api, inngest_client, [generate_morning_briefing])
