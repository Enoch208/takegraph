import { fetchDemoProof } from "@/lib/api";
import { landingTemplateHtml } from "@/lib/template-source";

/** Serve the landing as its native document instead of a nested scrolling
 * iframe. The proof values still come from the typed control-plane boundary. */
export async function GET() {
  const proof = await fetchDemoProof();

  return new Response(landingTemplateHtml(proof), {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "public, s-maxage=30, stale-while-revalidate=300",
    },
  });
}
