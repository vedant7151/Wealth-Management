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
    
    def fetch_profiles():
        db = Prisma()
        db.connect()
        profiles = db.clientprofile.find_many()
        db.disconnect()
        
        return [{"id": p.id, "themes": p.portfolio_themes} for p in profiles]
        
    profiles = await step.run("fetch_profiles", fetch_profiles)
    
    def run_agent(profile):
        input_state = {
            "client_id": profile["id"],
            "portfolio_themes": profile["themes"],
            "queries_to_run": [],
            "gathered_news": "",
            "iteration_count": 0,
            "revision_count": 0,
            "final_briefing": "",
            "final_citations": [],
            "is_compliant": False,
            "criticism": ""
        }
        
        accumulated = input_state.copy()
        for output in app.stream(input_state):
            for key, value in output.items():
                print(f"Finished node for {profile['id']}: {key}")
                if value:
                    accumulated.update(value)
        
        return accumulated.get("final_briefing", "Failed to generate briefing.")
        
    results = []
    for p in profiles:
        briefing = await step.run(f"run_agent_{p['id']}", lambda p=p: run_agent(p))
        results.append(len(briefing))
    
    return {"status": "success", "briefings_processed": len(results)}

# Create an ASGI app to serve the Inngest endpoints (FastAPI)
import fastapi
from inngest.fast_api import serve
api = fastapi.FastAPI(title="Wealth Agent Webhook Server")
serve(api, inngest_client, [generate_morning_briefing])
