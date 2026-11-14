import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { AnimatePresence, motion, MotionConfig } from 'motion/react';
import { ClerkProvider, SignedIn, SignedOut, SignInButton, UserButton, useAuth } from '@clerk/clerk-react';
import './index.css';
import { ThemeProvider } from './theme/ThemeProvider';
import { ThemeToggle } from './components/ThemeToggle';
import { ClientProfileCard } from './components/ClientProfileCard';
import { ClientActions } from './components/ClientActions';
import { ClientSwitcher } from './components/ClientSwitcher';
import { BriefingCard } from './components/BriefingCard';
import { AddClientForm } from './components/AddClientForm';
import { RunBriefingButton } from './components/RunBriefingButton';
import { RunProgress } from './components/RunProgress';
import { RunHistory } from './components/RunHistory';
import { HoldingsUpload } from './components/HoldingsUpload';
import { UsagePanel } from './components/UsagePanel';
import { createClient, fetchClientBriefings, fetchClients, fetchMyLimits, fetchServerDemoMode, startRun } from './lib/api';
import { CLERK_PUBLISHABLE_KEY, isClerkConfigured } from './auth/clerk';
import type { Briefing, Client, ClientBriefingsResponse, RunLimitInfo } from './types';

type GetToken = () => Promise<string | null>;

interface DashboardContentProps {
  getToken?: GetToken;
  authArea?: ReactNode;
}

function DashboardContent({ getToken, authArea }: DashboardContentProps) {
  const [clients, setClients] = useState<Client[]>([]);
  const [selectedClientId, setSelectedClientId] = useState<string | null>(null);
  const [briefingData, setBriefingData] = useState<ClientBriefingsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [clientsLoaded, setClientsLoaded] = useState(false);
  const [briefingError, setBriefingError] = useState<string | null>(null);
  const [runLimit, setRunLimit] = useState<RunLimitInfo | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runHistoryRefresh, setRunHistoryRefresh] = useState(0);
  const [serverDemoMode, setServerDemoMode] = useState<'true' | 'false' | 'auto' | null>(null);

  const reloadClients = useCallback(
    async (selectId?: string) => {
      const fetchedClients = await fetchClients(getToken);
      setClients(fetchedClients);
      setSelectedClientId(selectId ?? fetchedClients[0]?.id ?? null);
    },
    [getToken]
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const fetchedClients = await fetchClients(getToken);
        if (cancelled) return;
        setLoadError(null);
        setClients(fetchedClients);
        setSelectedClientId(fetchedClients[0]?.id ?? null);
      } catch (err) {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : 'Could not load clients.');
      }
      setClientsLoaded(true);
    })();
    fetchMyLimits(getToken).then((limits) => {
      if (!cancelled && limits) setRunLimit(limits.run);
    });
    fetchServerDemoMode().then((mode) => {
      if (!cancelled) setServerDemoMode(mode);
    });
    return () => {
      cancelled = true;
    };
    // getToken is intentionally excluded: Clerk returns a new function
    // identity on every render, which would otherwise refetch in a loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selectedClientId) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await fetchClientBriefings(selectedClientId, getToken);
        if (cancelled) return;
        setBriefingError(null);
        setBriefingData(data);
      } catch (err) {
        if (cancelled) return;
        setBriefingData(null);
        setBriefingError(err instanceof Error ? err.message : 'Could not load briefings.');
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedClientId]);

  const handleCreateClient = async (input: { name: string; portfolio_themes: string[] }) => {
    const created = await createClient(input, getToken);
    await reloadClients(created.id);
  };

  const handleClientUpdated = async (updated: Client) => {
    setClients((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
    setBriefingData((prev) => (prev && prev.client.id === updated.id ? { ...prev, client: updated } : prev));
  };

  const handleClientDeleted = async () => {
    setBriefingData(null);
    setActiveRunId(null);
    await reloadClients();
  };

  const handleRunBriefing = async () => {
    if (!selectedClientId) return;
    try {
      const { runId, runLimit: updatedLimit } = await startRun(selectedClientId, getToken);
      setActiveRunId(runId);
      if (updatedLimit) setRunLimit(updatedLimit);
    } finally {
      fetchMyLimits(getToken).then((limits) => {
        if (limits) setRunLimit(limits.run);
      });
    }
  };

  const handleRunDone = useCallback(
    async ({ status }: { status: string; briefingId: string | null }) => {
      setActiveRunId(null);
      setRunHistoryRefresh((n) => n + 1);
      if (status === 'success' && selectedClientId) {
        try {
          const data = await fetchClientBriefings(selectedClientId, getToken);
          setBriefingData(data);
        } catch (err) {
          setBriefingError(err instanceof Error ? err.message : 'Could not reload briefings.');
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selectedClientId]
  );

  const handleBriefingUpdated = useCallback((updated: Briefing) => {
    setBriefingData((prev) =>
      prev ? { ...prev, briefings: prev.briefings.map((b) => (b.id === updated.id ? updated : b)) } : prev
    );
  }, []);

  const lastUpdated = briefingData?.briefings[0]
    ? new Date(briefingData.briefings[0].createdAt).toLocaleDateString()
    : null;

  const briefingsLoading =
    !clientsLoaded || (selectedClientId !== null && briefingData?.client.id !== selectedClientId);

  return (
    <div className="layout">
      <header className="header">
        <div className="header-brand">
          <div className="header-mark" aria-hidden="true">
            WA
          </div>
          <div>
            <h1 className="header-title">Wealth Intelligence Agent</h1>
            <p className="header-subtitle">Autonomous research &amp; compliance briefings</p>
          </div>
        </div>
        <div className="header-actions">
          {authArea}
          <ThemeToggle />
        </div>
      </header>

      <main className="main-content">
        {loadError && (
          <p className="state-message" role="alert">
            {loadError}
          </p>
        )}

        {clientsLoaded && !loadError && clients.length === 0 && (
          <p className="state-message">No clients yet — use “Add client” to create one.</p>
        )}

        <AnimatePresence>
          {serverDemoMode === 'true' && (
            <motion.div
              className="sample-banner demo-mode-banner"
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.25 }}
            >
              Demo mode is active on this server — briefings are served from pre-recorded real runs, not live
              providers.
            </motion.div>
          )}
        </AnimatePresence>

        <div className="toolbar">
          {clients.length > 1 && selectedClientId && (
            <ClientSwitcher clients={clients} selectedId={selectedClientId} onSelect={setSelectedClientId} />
          )}
          <AddClientForm onCreate={handleCreateClient} />
        </div>

        {briefingData && (
          <ClientProfileCard
            client={briefingData.client}
            briefingCount={briefingData.briefings.length}
            lastUpdated={lastUpdated}
          />
        )}

        {briefingData && (
          <ClientActions
            client={briefingData.client}
            getToken={getToken}
            onUpdated={handleClientUpdated}
            onDeleted={handleClientDeleted}
          />
        )}

        {selectedClientId && <HoldingsUpload clientId={selectedClientId} getToken={getToken} />}

        <UsagePanel getToken={getToken} />

        {selectedClientId && <RunBriefingButton onRun={handleRunBriefing} runLimit={runLimit} />}

        {activeRunId && <RunProgress runId={activeRunId} getToken={getToken} onDone={handleRunDone} />}

        {selectedClientId && (
          <RunHistory
            clientId={selectedClientId}
            getToken={getToken}
            refreshToken={runHistoryRefresh}
            onRetry={handleRunBriefing}
            onSelectBriefing={(briefingId) => {
              const el = document.getElementById(`briefing-${briefingId}`);
              el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }}
          />
        )}

        <h2 className="section-heading">Morning Briefings</h2>

        {briefingError && (
          <p className="state-message" role="alert">
            {briefingError}
          </p>
        )}

        {briefingsLoading && !briefingData && !briefingError && <p className="state-message">Loading briefings…</p>}

        {briefingData && briefingData.briefings.length === 0 && (
          <p className="state-message">No briefings have been generated yet for this client.</p>
        )}

        <div className="briefings-list">
          <AnimatePresence mode="popLayout">
            {briefingData?.briefings.map((briefing, index) => (
              <BriefingCard
                key={briefing.id}
                briefing={briefing}
                index={index}
                getToken={getToken}
                onBriefingUpdated={handleBriefingUpdated}
              />
            ))}
          </AnimatePresence>
        </div>
      </main>

      <footer className="footer">
        © 2026 Wealth Intelligence Agent · SEC/FINRA Compliant Reporting Architecture
      </footer>
    </div>
  );
}

function AuthedDashboard() {
  const { getToken } = useAuth();

  return (
    <>
      <SignedOut>
        <div className="signed-out-gate">
          <div className="header-mark" aria-hidden="true">
            WA
          </div>
          <h1>Wealth Intelligence Agent</h1>
          <p>Sign in to view client portfolios and morning briefings.</p>
          <SignInButton mode="modal">
            <button type="button" className="run-briefing-button">
              Sign in
            </button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardContent getToken={getToken} authArea={<UserButton afterSignOutUrl="/" />} />
      </SignedIn>
    </>
  );
}

function App() {
  if (isClerkConfigured) {
    return (
      <ClerkProvider publishableKey={CLERK_PUBLISHABLE_KEY!}>
        <ThemeProvider>
          <MotionConfig reducedMotion="user">
            <AuthedDashboard />
          </MotionConfig>
        </ThemeProvider>
      </ClerkProvider>
    );
  }

  return (
    <ThemeProvider>
      <MotionConfig reducedMotion="user">
        <DashboardContent />
      </MotionConfig>
    </ThemeProvider>
  );
}

export default App;
