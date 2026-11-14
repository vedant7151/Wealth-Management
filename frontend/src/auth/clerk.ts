// Placeholder Clerk scaffold. Set VITE_CLERK_PUBLISHABLE_KEY in frontend/.env
// (see .env.example) once you have a real Clerk application. Until then,
// isClerkConfigured is false and App.tsx renders the dashboard directly,
// with no login gate.
export const CLERK_PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined;
export const isClerkConfigured = Boolean(CLERK_PUBLISHABLE_KEY);
