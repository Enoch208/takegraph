import { Hero } from "@/components/hero";
import { Nav } from "@/components/nav";
import { ProofStrip } from "@/components/proof-strip";
import { Architecture, FinalCta, Footer, HowItWorks } from "@/components/sections";
import { fetchDemoProof } from "@/lib/api";

/**
 * Landing page (PRD §18.5).
 *
 * A Server Component, so the proof figures are fetched on the server and the
 * client ships no data-fetching code. If the control plane is unreachable the
 * result carries the failure and the affected panels say so — the page does not
 * substitute placeholder numbers.
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
