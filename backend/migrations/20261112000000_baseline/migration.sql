-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "vector";

-- CreateEnum
CREATE TYPE "RunTrigger" AS ENUM ('manual', 'cron', 'demo');

-- CreateEnum
CREATE TYPE "RunStatus" AS ENUM ('running', 'success', 'failed');

-- CreateEnum
CREATE TYPE "BriefingStatus" AS ENUM ('draft', 'compliant', 'needs_human_review', 'failed', 'approved', 'rejected');

-- CreateTable
CREATE TABLE "ClientProfile" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "portfolio_themes" TEXT[],
    "clerkUserId" TEXT,

    CONSTRAINT "ClientProfile_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "Holding" (
    "id" TEXT NOT NULL,
    "clientProfileId" TEXT NOT NULL,
    "ticker" TEXT NOT NULL,
    "sector" TEXT,
    "weight" DOUBLE PRECISION NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "Holding_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "RunLog" (
    "id" TEXT NOT NULL,
    "clientProfileId" TEXT NOT NULL,
    "userId" TEXT,
    "trigger" "RunTrigger" NOT NULL,
    "status" "RunStatus" NOT NULL DEFAULT 'running',
    "startedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "finishedAt" TIMESTAMP(3),
    "durationMs" INTEGER,
    "nodeTimeline" JSONB,
    "errorCode" TEXT,
    "briefingId" TEXT,
    "idempotencyKey" TEXT,

    CONSTRAINT "RunLog_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "Briefing" (
    "id" TEXT NOT NULL,
    "content" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "clientProfileId" TEXT NOT NULL,
    "status" "BriefingStatus" NOT NULL DEFAULT 'draft',
    "complianceViolations" JSONB,
    "revisionCount" INTEGER NOT NULL DEFAULT 0,
    "runId" TEXT,
    "reviewedAt" TIMESTAMP(3),
    "reviewedBy" TEXT,
    "overrideReason" TEXT,
    "isDemo" BOOLEAN NOT NULL DEFAULT false,

    CONSTRAINT "Briefing_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "BriefingRevision" (
    "id" TEXT NOT NULL,
    "briefingId" TEXT NOT NULL,
    "revisionIndex" INTEGER NOT NULL,
    "content" TEXT NOT NULL,
    "violations" JSONB,
    "criticModel" TEXT,
    "isCompliant" BOOLEAN NOT NULL DEFAULT false,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "BriefingRevision_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "AuditLog" (
    "id" TEXT NOT NULL,
    "userId" TEXT,
    "clientId" TEXT,
    "action" TEXT NOT NULL,
    "briefingId" TEXT,
    "metadata" JSONB,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "AuditLog_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "Citation" (
    "id" TEXT NOT NULL,
    "url" TEXT NOT NULL,
    "title" TEXT,
    "publishedDate" TEXT,
    "briefingId" TEXT NOT NULL,

    CONSTRAINT "Citation_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "RateLimitHit" (
    "id" TEXT NOT NULL,
    "key" TEXT NOT NULL,
    "ts" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "RateLimitHit_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ApiCallLog" (
    "id" TEXT NOT NULL,
    "provider" TEXT NOT NULL,
    "model" TEXT,
    "purpose" TEXT NOT NULL,
    "runId" TEXT,
    "latencyMs" INTEGER NOT NULL,
    "status" TEXT NOT NULL,
    "estTokens" INTEGER,
    "estCostUsd" DOUBLE PRECISION,
    "cacheHit" BOOLEAN NOT NULL DEFAULT false,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ApiCallLog_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "SearchCache" (
    "id" TEXT NOT NULL,
    "query" TEXT NOT NULL,
    "queryEmbedding" vector(384) NOT NULL,
    "results" JSONB NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "SearchCache_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "DocumentChunk" (
    "id" TEXT NOT NULL,
    "url" TEXT NOT NULL,
    "title" TEXT,
    "publishedDate" TEXT,
    "chunkIndex" INTEGER NOT NULL,
    "content" TEXT NOT NULL,
    "embedding" vector(384) NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "DocumentChunk_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "DeliveryLog" (
    "id" TEXT NOT NULL,
    "briefingId" TEXT NOT NULL,
    "recipient" TEXT NOT NULL,
    "channel" TEXT NOT NULL DEFAULT 'email',
    "status" TEXT NOT NULL,
    "errorMessage" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "DeliveryLog_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "ClientProfile_clerkUserId_idx" ON "ClientProfile"("clerkUserId");

-- CreateIndex
CREATE INDEX "Holding_clientProfileId_idx" ON "Holding"("clientProfileId");

-- CreateIndex
CREATE INDEX "RunLog_clientProfileId_startedAt_idx" ON "RunLog"("clientProfileId", "startedAt");

-- CreateIndex
CREATE INDEX "RunLog_idempotencyKey_idx" ON "RunLog"("idempotencyKey");

-- CreateIndex
CREATE INDEX "Briefing_clientProfileId_idx" ON "Briefing"("clientProfileId");

-- CreateIndex
CREATE INDEX "BriefingRevision_briefingId_idx" ON "BriefingRevision"("briefingId");

-- CreateIndex
CREATE INDEX "AuditLog_briefingId_idx" ON "AuditLog"("briefingId");

-- CreateIndex
CREATE INDEX "AuditLog_clientId_idx" ON "AuditLog"("clientId");

-- CreateIndex
CREATE INDEX "Citation_briefingId_idx" ON "Citation"("briefingId");

-- CreateIndex
CREATE INDEX "RateLimitHit_key_ts_idx" ON "RateLimitHit"("key", "ts");

-- CreateIndex
CREATE INDEX "ApiCallLog_provider_createdAt_idx" ON "ApiCallLog"("provider", "createdAt");

-- CreateIndex
CREATE INDEX "ApiCallLog_runId_idx" ON "ApiCallLog"("runId");

-- CreateIndex
CREATE UNIQUE INDEX "DeliveryLog_briefingId_recipient_key" ON "DeliveryLog"("briefingId", "recipient");

-- AddForeignKey
ALTER TABLE "Holding" ADD CONSTRAINT "Holding_clientProfileId_fkey" FOREIGN KEY ("clientProfileId") REFERENCES "ClientProfile"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "RunLog" ADD CONSTRAINT "RunLog_clientProfileId_fkey" FOREIGN KEY ("clientProfileId") REFERENCES "ClientProfile"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "Briefing" ADD CONSTRAINT "Briefing_clientProfileId_fkey" FOREIGN KEY ("clientProfileId") REFERENCES "ClientProfile"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "BriefingRevision" ADD CONSTRAINT "BriefingRevision_briefingId_fkey" FOREIGN KEY ("briefingId") REFERENCES "Briefing"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "AuditLog" ADD CONSTRAINT "AuditLog_briefingId_fkey" FOREIGN KEY ("briefingId") REFERENCES "Briefing"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "Citation" ADD CONSTRAINT "Citation_briefingId_fkey" FOREIGN KEY ("briefingId") REFERENCES "Briefing"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "DeliveryLog" ADD CONSTRAINT "DeliveryLog_briefingId_fkey" FOREIGN KEY ("briefingId") REFERENCES "Briefing"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

