import { Hero } from "@/components/hero";
import { Nav } from "@/components/nav";
import { ProofStrip } from "@/components/proof-strip";
import { Architecture, FinalCta, Footer, HowItWorks } from "@/components/sections";
import { fetchDemoProof } from "@/lib/api";

/**
 * Landing page (PRD §18.5).
 *
 * A Server Component, so the proof figures are fetched server-side and the
 * client ships no data-fetching code for them. Every number on this page comes
 * from GET /api/v1/demo/proof, which reads a real build's persisted events when
 * one exists and otherwise reports a projection — §4.4 forbids hard-coding seed
 * metrics in React, and the response's `source` field is what lets the UI say
 * which it is looking at.
 *
 * If the control plane is unreachable, the affected panels render the failure.
 * They do not substitute placeholder numbers (§0.1).
 */
export default async function LandingPage() {
  const proof = await fetchDemoProof();

  return (
    <>
      <Nav />
      <main id="main">
        <Hero proof={proof} />
        <ProofStrip proof={proof} />
        <HowItWorks proof={proof} />
        <Architecture />
        <FinalCta />
      </main>
      <Footer />
    </>
  );
}
