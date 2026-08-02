import { landingTemplateHtml } from "@/lib/template-source";

/**
 * Render the supplied Aura export in its own document so its complete CSS,
 * scripts, fixed navigation, scroll observers and Unicorn canvas behave exactly
 * as authored instead of being partially reinterpreted by the app shell.
 */
export default function LandingPage() {
  return (
    <main id="main" className="h-dvh w-full overflow-hidden bg-black">
      <iframe
        title="Creative landing page"
        srcDoc={landingTemplateHtml()}
        className="block h-full w-full border-0 bg-black"
      />
    </main>
  );
}
