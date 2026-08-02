import templateExport from "../../../t.json";
import { icons, type IconName } from "@/components/icon";
import type { DemoProof, Result } from "@/lib/api";

type TemplateExport = Array<{ code: string }>;

function readTemplateCode(): string {
  const code = (templateExport as TemplateExport)[0]?.code;
  if (!code) throw new Error("t.json does not contain a landing-page export");
  return code;
}

const templateCode = readTemplateCode();

const HERO_DEPENDENCY_GRAPH = `<div id="tg-hero-graph" aria-hidden="true"><svg viewBox="0 0 1440 820" preserveAspectRatio="xMidYMid slice" role="presentation"><g class="tg-base-edges"><path d="M90 280 C220 280 210 170 350 170"/><path d="M90 280 C230 280 220 330 350 330"/><path d="M90 510 C220 510 220 430 350 430"/><path d="M350 170 C500 170 490 110 630 110"/><path d="M350 170 C500 170 490 215 630 215"/><path d="M350 330 C500 330 490 320 630 320"/><path d="M350 430 C500 430 490 425 630 425"/><path d="M630 110 C790 110 780 155 930 155"/><path d="M630 215 C790 215 780 255 930 255"/><path d="M630 320 C790 320 780 355 930 355"/><path d="M630 425 C790 425 780 455 930 455"/><path d="M930 155 C1090 155 1080 320 1230 320"/><path d="M930 255 C1090 255 1080 340 1230 340"/><path d="M930 355 C1090 355 1080 360 1230 360"/><path d="M930 455 C1090 455 1080 380 1230 380"/></g><g class="tg-reused-edges"><path d="M90 280 C220 280 210 170 350 170"/><path d="M350 170 C500 170 490 110 630 110"/><path d="M350 170 C500 170 490 215 630 215"/><path d="M630 110 C790 110 780 155 930 155"/><path d="M630 215 C790 215 780 255 930 255"/></g><g class="tg-affected-edges"><path pathLength="1" d="M90 510 C220 510 220 430 350 430"/><path pathLength="1" d="M350 430 C500 430 490 425 630 425"/><path pathLength="1" d="M630 425 C790 425 780 455 930 455"/><path pathLength="1" d="M930 455 C1090 455 1080 380 1230 380"/></g><g class="tg-nodes"><circle class="tg-node-reused" cx="90" cy="280" r="5"/><circle class="tg-node-review" cx="90" cy="510" r="6"/><circle class="tg-node-reused" cx="350" cy="170" r="5"/><circle class="tg-node-reused" cx="350" cy="330" r="5"/><circle class="tg-node-affected" cx="350" cy="430" r="7"/><circle class="tg-node-reused" cx="630" cy="110" r="5"/><circle class="tg-node-reused" cx="630" cy="215" r="5"/><circle class="tg-node-running" cx="630" cy="320" r="6"/><circle class="tg-node-affected" cx="630" cy="425" r="7"/><circle class="tg-node-reused" cx="930" cy="155" r="5"/><circle class="tg-node-reused" cx="930" cy="255" r="5"/><circle class="tg-node-running" cx="930" cy="355" r="6"/><circle class="tg-node-affected" cx="930" cy="455" r="7"/><circle class="tg-node-affected" cx="1230" cy="380" r="8"/></g></svg></div>`;

const LANDING_PERFORMANCE_HEAD = `<link rel="icon" href="/icon.png" type="image/png"><style id="takegraph-performance">
html{scroll-behavior:smooth;scroll-padding-top:112px;background:#050608}
body{position:relative;overscroll-behavior-y:none;background:#050608!important}
.gradient-blur>div,.gradient-blur::before,.gradient-blur::after{display:none!important}
.gradient-blur{background:linear-gradient(to bottom,rgba(5,6,8,.88),rgba(5,6,8,.3),transparent);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);mask-image:linear-gradient(to bottom,black 0%,black 42%,transparent 100%)}
[data-us-project="sajpUiTp7MIKdX6daDCu"]{opacity:.5!important;filter:brightness(.52) saturate(1.18) sepia(.12) hue-rotate(8deg)}
body>.aura-background-component::after{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(to bottom,rgba(5,6,8,.32),rgba(5,6,8,.7)),radial-gradient(ellipse at 50% 26%,rgba(255,106,53,.18),transparent 60%)}
#tg-hero-graph{position:absolute;inset:0 0 auto;width:100%;height:880px;z-index:0;pointer-events:none;overflow:hidden;opacity:.2;mix-blend-mode:screen;mask-image:radial-gradient(ellipse 82% 72% at 50% 38%,black 18%,transparent 82%)}
#tg-hero-graph svg{width:100%;height:100%}
#tg-hero-graph path{fill:none;vector-effect:non-scaling-stroke}
.tg-base-edges path{stroke:#2A3039;stroke-width:1}
.tg-reused-edges path{stroke:#39D98A;stroke-width:1.15;opacity:.36}
.tg-affected-edges path{stroke:#FF6A35;stroke-width:2;stroke-linecap:round;stroke-dasharray:.045 .955;animation:tgGraphFlow 3.8s linear infinite;filter:drop-shadow(0 0 5px rgba(255,106,53,.7))}
.tg-nodes circle{stroke:#050608;stroke-width:2;vector-effect:non-scaling-stroke}
.tg-node-reused{fill:#39D98A}.tg-node-running{fill:#67A7FF}.tg-node-review{fill:#F5C451}.tg-node-affected{fill:#FF6A35;filter:drop-shadow(0 0 7px rgba(255,106,53,.8))}
body>section:not(:first-of-type),body>footer{content-visibility:auto;contain-intrinsic-size:auto 900px}
body>section:first-of-type>.z-10>div.flex>button{min-width:260px!important;padding-inline:28px!important;flex:0 0 auto;overflow:hidden}
body>section:first-of-type>.z-10>div.flex>button span{white-space:nowrap}
.tg-offscreen *{animation-play-state:paused!important}
@keyframes tgGraphFlow{to{stroke-dashoffset:-1}}
@media(max-width:640px){body>section:first-of-type>.z-10>div.flex{width:100%}body>section:first-of-type>.z-10>div.flex>button{width:100%;min-width:0!important;max-width:360px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.tg-offscreen *{animation:none!important}.tg-affected-edges path{animation:none;stroke-dasharray:none;opacity:.8}}
</style>`;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function replaceTextNode(source: string, from: string, to: string): string {
  const pattern = escapeRegExp(from).replace(/\s+/g, "\\s+");
  return source.replace(new RegExp(`(>\\s*)${pattern}(\\s*<)`, "g"), (_match, open, close) => {
    return `${open}${to}${close}`;
  });
}

function replaceTextNodeOnce(source: string, from: string, to: string): string {
  const pattern = escapeRegExp(from).replace(/\s+/g, "\\s+");
  return source.replace(new RegExp(`(>\\s*)${pattern}(\\s*<)`), (_match, open, close) => {
    return `${open}${to}${close}`;
  });
}

function dedupeHtmlFragments(source: string, pattern: RegExp): string {
  const seen = new Set<string>();
  return source.replace(pattern, (fragment) => {
    if (seen.has(fragment)) return "";
    seen.add(fragment);
    return fragment;
  });
}

function adaptTakegraphContent(source: string, proof: Result<DemoProof>): string {
  const data = proof.ok ? proof.data : null;
  const totalNodes = data ? String(data.total_nodes) : "UNAVAILABLE";
  const reused = data ? String(data.reuse) : "UNAVAILABLE";
  const rebuilt = data ? String(data.rebuild) : "UNAVAILABLE";
  const providerCalls = data ? String(data.provider_calls) : "UNAVAILABLE";
  const pricing = data ? data.estimated_cost_usd ?? data.pricing_status : "UNAVAILABLE";
  const proofLabel = data
    ? data.verified_build
      ? "REPLAY OF REAL RUN"
      : "TEMPLATE PROJECTION"
    : "CONTROL PLANE UNAVAILABLE";

  const replacements: ReadonlyArray<readonly [string, string]> = [
    ["Creative - Digital Designer &amp; Developer", "TAKEGRAPH — Selective rebuilding for generative media"],
    ["Available for new projects", `${proofLabel} · ORBIT`],
    ["your", "one"],
    ["vision", "detail."],
    ["with", "Rebuild"],
    ["creative", "only what"],
    ["excellence", "changed."],
    [
      "Crafting digital experiences that merge art and technology. From branding to web development, I build it all.",
      "TAKEGRAPH keeps multimodel media productions consistent, recoverable, and verifiable.",
    ],
    ["VIEW PORTFOLIO", "OPEN LIVE BUILD"],
    ["CONTACT ME", "SEE CHANGE PROPAGATE"],
    ["Book Call", "Open Live Build"],
    ["Active Projects", "Seed Graph"],
    ["▲ 12%", proofLabel],
    ["Global deployment across 3 regions.", "Compiled from canonical source, policy, and dependency inputs."],
    ["Client Retention", "Impact Preview"],
    ["Based on annual recurring contracts.", `Legal-copy revision · ${proofLabel.toLowerCase()}.`],
    ["View Dashboard", "Preview Impact"],
    ["Neon Nexus", "ORBIT Hydration"],
    ["Fintech", data ? `${totalNodes}-node graph` : "Graph unavailable"],
    ["Web App", "16:9 + 9:16"],
    ["2024", "Release v1"],
    [
      "Redefining digital finance with a brutalist interface and real-time data visualization engine.",
      "A cinematic hydration launch package with every source, attempt, quality decision, stored byte, and release asset connected by lineage.",
    ],
    ["Capabilities Index", "Build Capabilities"],
    ["Digital Strategy", "Generate"],
    ["UI/UX Design", "Validate"],
    ["Development", "Recover"],
    ["git", "Genblaze"],
    ["Lucidchart", "Backblaze B2"],
    ["wrike", "PostgreSQL"],
    ["jQuery", "ElevenLabs"],
    ["openstack", "GMI Cloud"],
    ["servicenow", "FFmpeg"],
    ["paysafe:", "SHA-256"],
    ["01.FEATURED CASE STUDY", "01. LIVE BUILD"],
    ["Products", "ORBIT"],
    ["Real Products", "A build you can interrupt."],
    [
      "A selected case study showcasing how we design and build scalable digital products, driven by performance, data, and real business impact.",
      "Change one approved phrase, preview the exact blast radius, confirm a plan-hash-bound rebuild, refresh mid-run, and return to the same durable state.",
    ],
    ["View Case Study", "Open ORBIT Build"],
    ["Stats", "Build Impact"],
    ["New User", "Selective execution"],
    ["Last 6 months", "Legal-copy revision"],
    ["Overall - 80%", `${reused} reuse · ${rebuilt} rebuild`],
    ["App Install", "Reused nodes"],
    ["App open", "Rebuilt nodes"],
    ["Sign Up", "Provider calls"],
    ["Home page", "Pricing status"],
    ["Top Content", "Build Event Stream"],
    ["Last 14 days", "Persisted, ordered, resumable"],
    ["Estimated revenue", "Estimated provider cost"],
    ["Asset Allocation", "Impact decision"],
    ["Build an Interactive site", "Apply legal-copy revision"],
    ["Nexus Finance App", "ORBIT incremental build"],
    ["Total Users", "Total graph nodes"],
    ["Creative Agency", "TAKEGRAPH Control Plane"],
    ["Digital Product Studio · est. 2012", "Selective media rebuild engine · event sourced"],
    ["Projects", "Nodes"],
    ["Awards", "Reused"],
    ["Offices", "Rebuilt"],
    [
      "We craft digital experiences that merge art, technology, and strategy. Our approach is rooted in rigorous design systems and future-ready engineering to help brands thrive in the modern economy.",
      "Every output is fingerprinted from what actually produced it. TAKEGRAPH keeps valid assets, classifies failures, stores provider output durably, and publishes release evidence that can be checked again later.",
    ],
    ["Agency Reel", "Watch Build Flow"],
    ["LinkedIn", "Lineage"],
    ["Behance", "Manifest"],
    ["Start a Project", "Open Live Build"],
    ["Recent Work", "Critical Demo Paths"],
    ["View All Projects", "Inspect Proof"],
    ["Vertex AI", "TEST FAULT recovery"],
    ["Echo Platform", "Identity-gate retake"],
    ["Product Design", "Rejected attempt preserved · custom image needed"],
    ["01. CAPABILITIES", "02. CAPABILITIES"],
    ["Expertise", "System"],
    ["Digital Expertise", "Built for causality"],
    [
      "A comprehensive suite of design and engineering services. We build scalable digital products for ambitious brands.",
      "The system explains why a node is reused, rebuilt, blocked, retried, or routed across providers—and preserves the evidence behind every decision.",
    ],
    ["View Services", "Explore Architecture"],
    ["Visual systems that tell your story. Logos, color palettes, and typography.", "Canonical fingerprints make reuse deterministic across builds and restarts."],
    ["Web Design", "Impact Planning"],
    ["Immersive web experiences designed to convert visitors into loyal customers.", "Preview the blast radius and provider-call count before spending anything."],
    ["Defining the visual language, photography, and tone for your brand.", "Technical, specification, and identity checks attach inspectable evidence to attempts."],
    ["Strategy", "Durable Recovery"],
    ["Data-driven insights to position your brand for market leadership.", "Leases, idempotency, typed failures, and bounded fallback survive worker interruption."],
    ["Content / Motion", "Verifiable Releases"],
    ["High-quality asset creation, from photography to 3D motion design.", "Stored-byte SHA-256, manifests, approval history, and retention readback make releases checkable."],
    ["Instrument Serif", "NODE_SPEC_CHANGED"],
    ["Component", "Plan hash"],
    ["Confirm", "Confirm build"],
    ["Cancel", "Preview only"],
    ["Growth Rate", "Reuse ratio"],
    ["02. HOW IT WORKS", "03. HOW IT WORKS"],
    ["Work", "Product"],
    ["Services", "How It Works"],
    ["About", "Architecture"],
    ["Pricing", "Proof"],
    ["How We Work", "From change to proof"],
    [
      "A transparent, step-by-step workflow designed to make collaboration seamless and results predictable. From chaos to clarity.",
      "A deterministic path from approved source change to selective execution, bounded recovery, quality review, and independently verifiable release.",
    ],
    ["Discovery", "Compile"],
    ["We audit your goals, users, and constraints, then define what “success” looks like.", "Canonicalize the source and compile a typed dependency graph with stable node identities."],
    ["We map the user journey and technical architecture, so every screen has a purpose.", "Fingerprint every node from its spec, inputs, provider policy, and relevant upstream outputs."],
    ["Design", "Preview"],
    ["We craft high-fidelity UI and clickable prototypes, then iterate fast with your feedback.", "Compare fingerprints and show REUSE or REBUILD with a reason code before provider calls begin."],
    ["Build", "Execute"],
    ["We ship production-ready code, optimized for performance, SEO, and clean handoff.", "Run only invalidated nodes, store bytes in B2, verify hashes, and stream durable state transitions."],
    ["Launch", "Publish"],
    ["We deploy, QA, and track results. You get analytics, documentation, and a smooth handoff.", "Approve with a reason, publish an atomic release pointer, and verify the manifest and retention state again."],
    ["03. TESTIMONIALS", "04. BUILD EVIDENCE"],
    ["passionate", "inspectable"],
    ["Loved by", "Evidence is"],
    ["Everything you need to create, collaborate, and convert. Built for modern teams.", "No invented customer praise: these cards show the actual decisions, failure classes, lineage, and verification facts the demo is designed to prove."],
    ["View Portfolio", "Open Evidence View"],
    ['"This platform completely changed how we approach design systems. The speed and consistency is mind-blowing."', data ? `A legal-copy change invalidates exactly ${rebuilt} nodes; ${reused} remain valid and are reused.` : "Impact evidence is unavailable until the control plane responds."],
    ['"Just shipped my portfolio using the new components. Detail is insane. 🚀"', "Every rebuild decision includes a reason code and the upstream fingerprint that changed."],
    ['"Accessibility features saved us weeks. Rare to find a kit this robust."', "A TEST FAULT timeout creates a parent-linked cross-provider child attempt with its routing reason."],
    ['"I\'ve used every UI kit out there. Nothing comes close to the polish and flexibility of this one."', "A failed product-identity attempt stays playable after a bounded retake is created."],
    ['"Finally, a tool that bridges the gap between design and code perfectly."', "Provider output is downloaded, hashed from stored bytes, and written to Backblaze B2 before it can pass."],
    ['"Refactoring legacy apps was daunting until we adopted this system."', "Refreshing mid-build reconstructs the same state from durable events instead of losing progress."],
    ['"We redesigned our entire SaaS dashboard in a weekend. Robust and accessible."', "Publishing records the approver, reason, time, manifest, SHA-256 values, and retention readback."],
    ['"Best investment we made for our design team this year. ROI was immediate."', "Restoring release v1 flips one atomic pointer and makes zero provider calls."],
    ['"I\'ve cancelled 3 other subscriptions. This is the only UI kit I need."', "ERROR is never displayed as PASS, and unavailable generation never silently switches to a fixture."],
    ["Sarah Jenkins", "IMPACT_PLAN_CREATED"],
    ["Marcus T.", "NODE_REUSED"],
    ["Michael Chen", "ATTEMPT_FAILED"],
    ["Sofia Davis", "ASSET_VERIFIED"],
    ["David Kim", "RELEASE_PUBLISHED"],
    ["Alex Morgan", "RELEASE_RESTORED"],
    ["@jamesbuilds", "plan_hash bound"],
    ["Product Director", "AUDIT EVENT"],
    ["03. PRICING", "05. OPERATING MODES"],
    ["Simple, transparent", "Truthful at every"],
    ["pricing", "stage"],
    ["Choose the perfect plan for your business needs. Pause or cancel anytime.", "Fixture, live, and release-proof modes are explicit. A missing credential never turns a production action into a fake success."],
    ["Growth", "Fixture Lab"],
    ["Perfect for growing businesses with steady design needs.", "Deterministic local testing for parsers, impact logic, state machines, and failure injection."],
    ["Monthly", "Local"],
    ["Yearly", "Deterministic"],
    ["Pause or cancel anytime.", "Clearly labeled fixture mode."],
    ["Get Started", "Open Live Build"],
    ["What's included", "What it proves"],
    ["45 hours of dedicated design time", "Canonicalization and fingerprint determinism"],
    ["Two active projects at a time", data ? `Golden ${reused} reuse / ${rebuilt} rebuild impact test` : "Golden impact test · control plane unavailable"],
    ["Twice-weekly syncs", "Cycle rejection and reason-code coverage"],
    ["24-hour response time", "Failure injection scoped to the demo project"],
    ["Scale", "Live Build"],
    ["Most Popular", "DEMO PATH"],
    ["For teams that need to move fast and ship often.", "Real provider attempts, durable storage, SSE updates, quality gates, and bounded recovery."],
    ["Everything in Growth, plus:", "What it proves"],
    ["100 hours of dedicated design time", "Genblaze attempt lineage and routing policy"],
    ["Unlimited active projects", "Backblaze B2 stored-byte verification"],
    ["Daily syncs available", "Refresh-safe durable queue and event log"],
    ["Same-day response time", "Explicit unavailable capability reporting"],
    ["Custom", "Release Proof"],
    ["Clear scope, fixed timeline, no surprises.", "Approved, hash-verified, retention-aware, and independently re-checkable."],
    ["Starts at", "Evidence"],
    ["Book a Call", "Inspect Release"],
    ["Sarah Park", "Release manifest"],
    ["Project Manager", "SHA-256 · APPROVAL · RETENTION"],
    ['"We\'ll help you choose the right plan and get you started within 3-5 days."', "Verify stored assets, approval history, manifest integrity, and the currently published release pointer."],
    ["2 spots left for", "Status read back from"],
    ["July", "B2"],
    ["Ready to launch your", "Ready to change one"],
    ["vision?", "detail?"],
    ["I work with brands that believe in quality design. Let's build something amazing together.", data ? `Open the seeded ORBIT production, edit one approved line, and watch TAKEGRAPH preserve ${reused} valid nodes.` : "Open the seeded ORBIT production once the control plane is available and inspect every reuse or rebuild decision."],
    ["Project Type / Budget", "Production / template"],
    ["Anything we should know?", "What needs to stay consistent?"],
    ["Start Conversation", "Open Live Build"],
    ["We offer clarity and collaboration tools to help teams effectively plan, track, and launch digital products.", "Selective rebuilding, durable recovery, quality evidence, and verifiable releases for multimodel media production."],
    ["Map", "Product"],
    ["FEATURES", "CAPABILITIES"],
    ["SERVICES", "HOW IT WORKS"],
    ["REVIEWS", "BUILD EVIDENCE"],
    ["FAQS", "SYSTEM STATUS"],
    ["Company", "Demo"],
    ["HOME", "LANDING"],
    ["ABOUT", "ARCHITECTURE"],
    ["PRICING", "OPERATING MODES"],
    ["CONTACT", "LIVE BUILD"],
    ["PRIVACY POLICY", "PRIVACY"],
    ["TERMS &amp; CONDITIONS", "TERMS"],
    ["CREATIVE", "TAKEGRAPH"],
    ["Creative Team", "TAKEGRAPH"],
    ["Made with love by", "Built to cause, remember, and verify by"],
    ["Creative", "TAKEGRAPH"],
  ];

  let adapted = replaceTextNodeOnce(source, "Design", "Change");
  adapted = replaceTextNodeOnce(adapted, "Brand Identity", "Release");
  adapted = replaceTextNodeOnce(adapted, "Brand Identity", "Cross-provider lineage · custom image needed");
  adapted = replaceTextNodeOnce(adapted, "Brand Identity", "Deterministic Fingerprints");
  adapted = replaceTextNodeOnce(adapted, "Featured Case Study", "CUSTOM IMAGE NEEDED");
  adapted = replaceTextNodeOnce(adapted, "Featured Case Study", proofLabel);
  for (const [from, to] of replacements) adapted = replaceTextNode(adapted, from, to);

  const numberReplacements: ReadonlyArray<readonly [string, string]> = [
    ["24", totalNodes],
    ["98", reused],
    [".4%", " REUSED"],
    ["142", totalNodes],
    ["28", reused],
    ["4", rebuilt],
    ["2.4M+", totalNodes],
    ["46K", reused],
    ["41K", rebuilt],
    ["30K", providerCalls],
    ["$6.295,29", pricing],
    ["$ 157.49", `${reused} reuse / ${rebuilt} rebuild`],
    ["3,000", "TEST"],
    ["5,000", "LIVE"],
    ["6k", "SHA-256"],
    ["/ mo", "MODE"],
    ["$", ""],
  ];
  for (const [from, to] of numberReplacements) adapted = replaceTextNode(adapted, from, to);

  adapted = adapted
    .replace(
      /<div class="grid grid-cols-2[^"]*(?:w-6 h-6|w-8 h-8)[^"]*">\s*(?:<div[^>]*><\/div>\s*){4}<\/div>/g,
      (block) => {
        const size = block.includes("w-8 h-8") ? "w-10 h-10" : "w-7 h-7";
        return `<img src="/brand/mark.png" alt="" class="${size} shrink-0 object-contain">`;
      },
    )
    .replace(/<link id="all-fonts-link-font-(?!manrope"|oswald")[^>]*>/g, "")
    .replace(/<style id="all-fonts-style-font-(?!manrope"|oswald")[^"]*">[\s\S]*?<\/style>/g, "")
    .replace(/<script>\s*\/\*\s*Sequence animation on scroll[\s\S]*?<\/script>/i, "")
    .replace(
      /<style>\s*\/\* Default: paused \*\/[\s\S]*?\.animate-on-scroll\.animate \{ animation-play-state: running !important; \}\s*<\/style>/g,
      "",
    )
    .replaceAll("#ef233c", "#FF6A35")
    .replaceAll("#dc2626", "#FF6A35")
    .replaceAll("#d90429", "#FF6A35")
    .replaceAll("rgba(239,35,60", "rgba(255,106,53")
    .replaceAll("rgba(239, 35, 60", "rgba(255, 106, 53")
    .replaceAll("rgba(220,38,38", "rgba(255,106,53")
    .replaceAll("rgba(220, 38, 38", "rgba(255, 106, 53")
    .replaceAll("red-600", "[#FF6A35]")
    .replaceAll("red-500", "[#FF6A35]")
    .replaceAll("red-200", "[#FFD7C8]")
    .replaceAll("red-100", "[#FFD7C8]")
    .replace(/(<body[^>]*>)/i, `$1${HERO_DEPENDENCY_GRAPH}`)
    .replace(/https:\/\/images\.unsplash\.com\/photo-[^"']+\?w=(?:100|150)&amp;h=(?:100|150)&amp;fit=crop/g, "/brand/mark.png")
    .replace(/alt=["'](?:Sarah|Marcus|Michael|Sofia|David|Alex|Profile)["']/g, 'alt="TAKEGRAPH build evidence"')
    .replace('alt="Agency Hero"', 'alt="Placeholder for the ORBIT cinematic master"')
    .replace('alt="Dark abstract gradient"', 'alt="Placeholder for cross-provider fallback evidence"')
    .replace('alt="Abstract architectural forms"', 'alt="Placeholder for identity-gate retake evidence"')
    .replace(/\s+onclick="updatePricing\([^"]+\)"/g, "")
    .replace(/<script>\s*function updatePricing\([\s\S]*?<\/script>/i, "")
    .replace("window.location.href='/home'", "window.parent.location.href='/'")
    .replace("window.location.href='/bookcall'", "window.parent.location.href='/demo'")
    .replace('href="/work"', 'href="#proof"')
    .replace('href="/services"', 'href="#workflow"')
    .replace('href="/about"', 'href="#capabilities"')
    .replace('href="/pricing"', 'href="#evidence"')
    .replace("01. LIVE BUILD", '<span id="proof">01. LIVE BUILD</span>')
    .replace("02. CAPABILITIES", '<span id="capabilities">02. CAPABILITIES</span>')
    .replace("03. HOW IT WORKS", '<span id="workflow">03. HOW IT WORKS</span>')
    .replace("04. BUILD EVIDENCE", '<span id="evidence">04. BUILD EVIDENCE</span>')
    .replace("05. OPERATING MODES", '<span id="modes">05. OPERATING MODES</span>')
    .replace(
      /(<h2[^>]*>)\s*Real\s*<span[^>]*>\s*ORBIT\s*<\/span>\s*(<\/h2>)/i,
      "$1A build you can interrupt.$2",
    )
    .replace(
      /(<h2[^>]*>)\s*Digital\s*<span[^>]*>\s*System\s*<\/span>\s*(<\/h2>)/i,
      "$1Built for causality$2",
    )
    .replace(
      /(<h2[^>]*>)\s*How We\s*<span[^>]*>\s*Product\s*<\/span>\s*(<\/h2>)/i,
      "$1From change to proof$2",
    )
    .replace(
      "</head>",
      `${LANDING_PERFORMANCE_HEAD}</head>`,
    )
    .replace(
      "</body>",
      `<script>var tgSections=document.querySelectorAll("body>section,body>footer");var tgObserver=new IntersectionObserver(function(entries){entries.forEach(function(entry){entry.target.classList.toggle("tg-offscreen",!entry.isIntersecting)})},{rootMargin:"400px 0px"});tgSections.forEach(function(section){tgObserver.observe(section)});document.addEventListener("click",function(event){var button=event.target.closest("button");if(!button)return;var label=(button.textContent||"").replace(/\\s+/g," ").trim().toUpperCase();var demo=["OPEN LIVE BUILD","PREVIEW IMPACT","OPEN ORBIT BUILD","MORE","OPEN EVIDENCE VIEW","INSPECT RELEASE"];var sections={"SEE CHANGE PROPAGATE":"workflow","WATCH BUILD FLOW":"workflow","EXPLORE ARCHITECTURE":"capabilities","LINEAGE":"evidence","MANIFEST":"evidence","INSPECT PROOF":"evidence"};if(demo.includes(label)){window.location.href="/demo";return}if(sections[label]){document.getElementById(sections[label])?.scrollIntoView({behavior:"smooth",block:"start"});return}if(["LOCAL","DETERMINISTIC","CONFIRM BUILD","PREVIEW ONLY"].includes(label)){event.preventDefault();button.disabled=true;button.setAttribute("aria-disabled","true")}});</script></body>`,
    );

  adapted = dedupeHtmlFragments(
    adapted,
    /<link id="all-fonts-link-font-(?:manrope|oswald)"[^>]*>/g,
  );
  adapted = dedupeHtmlFragments(
    adapted,
    /<style id="all-fonts-style-font-(?:manrope|oswald)">[\s\S]*?<\/style>/g,
  );

  return adapted;
}

const ICONS: Record<string, IconName> = {
  "arrow-right": "arrowRight",
  "arrow-up-right": "arrowUpRight",
  "chevron-right": "chevronRight",
  bell: "notification",
  globe: "globe",
  instagram: "instagram",
  play: "play",
  plus: "add",
  twitter: "twitter",
};

function classNameFrom(attributes: string): string {
  const className = attributes.match(/\bclass=["']([^"']*)["']/i)?.[1] ?? "";
  const withoutSourceRuntimeClasses = className
    .split(/\s+/)
    .filter((token) => token && token !== "lucide" && !token.startsWith("lucide-") && !token.startsWith("iconify"))
    .join(" ");
  return withoutSourceRuntimeClasses || "size-4";
}

function iconFor(attributes: string, contents: string): IconName {
  const lucideName = attributes.match(/\blucide-([a-z0-9-]+)/i)?.[1];
  if (lucideName && ICONS[lucideName]) return ICONS[lucideName];

  if (/data-icon=/i.test(attributes)) return "provider";
  if (/fill-current/i.test(attributes)) return "star";
  if (/translate-x|M5 12h14|M9 5l7 7|m9 18 6-6/i.test(attributes + contents)) {
    return "arrowRight";
  }
  if (/M7 17L17 7|arrow-up/i.test(attributes + contents)) return "arrowUpRight";
  if (/check|M20 6L9 17/i.test(attributes + contents)) return "verified";
  return "generate";
}

type HugeIconElement = readonly [string, Readonly<Record<string, string | number>>];

function escapeAttribute(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function attributeName(name: string): string {
  return name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}

function hugeIcon(name: IconName, className: string): string {
  const elements = icons[name] as unknown as readonly HugeIconElement[];
  const body = elements
    .map(([tag, attributes]) => {
      const serialized = Object.entries(attributes)
        .filter(([attribute]) => attribute !== "key")
        .map(([attribute, value]) => {
          const normalizedValue = attribute === "strokeWidth" ? "1.65" : String(value);
          return `${attributeName(attribute)}="${escapeAttribute(normalizedValue)}"`;
        })
        .join(" ");
      return `<${tag} ${serialized}></${tag}>`;
    })
    .join("");

  return `<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" color="currentColor" class="${escapeAttribute(className)}" aria-hidden="true">${body}</svg>`;
}

function replaceTemplateIcons(source: string): string {
  const withoutIconRuntimes = source
    .replace(/<script src="https:\/\/unpkg\.com\/lucide@latest"><\/script>\s*/i, "")
    .replace(/<script src="https:\/\/code\.iconify\.design\/3\/3\.1\.0\/iconify\.min\.js"><\/script>\s*/i, "")
    .replace(/<script>\s*lucide\.createIcons\(\);\s*<\/script>/gi, "")
    .replace(
      /<script[^>]*>\s*if \(typeof lucide !== ["']undefined["']\) \{\s*lucide\.createIcons\(\);\s*\}\s*<\/script>/gi,
      "",
    )
    .replace(/\s*lucide\.createIcons\(\);/gi, "");

  const withRenderedLucideTags = withoutIconRuntimes.replace(
    /<i\b([^>]*\bdata-lucide=["']([a-z0-9-]+)["'][^>]*)><\/i>/gi,
    (_tag, attributes: string, lucideName: string) =>
      hugeIcon(ICONS[lucideName] ?? "generate", classNameFrom(attributes)),
  );

  const withRenderedIconifySpans = withRenderedLucideTags.replace(
    /<span\b([^>]*\bdata-icon=["'][^"']+["'][^>]*)><\/span>/gi,
    (_tag, attributes: string) => hugeIcon("provider", classNameFrom(attributes)),
  );

  return withRenderedIconifySpans.replace(
    /<svg\b([^>]*)>([\s\S]*?)<\/svg>/gi,
    (svg, attributes: string, contents: string) => {
      const isIcon =
        /\bviewBox=["']0 0 24 24["']/i.test(attributes) ||
        /\blucide-|\bdata-icon=/i.test(attributes);

      if (!isIcon) return svg;
      return hugeIcon(iconFor(attributes, contents), classNameFrom(attributes));
    },
  );
}

/**
 * The JSON export is the visual source of truth. The only deliberate changes
 * are non-visual: remove Aura's cross-frame referral write and swap its icon
 * runtimes/markup for the project's HugeIcons registry. The Unicorn scene,
 * Tailwind runtime, typography, imagery, layout, animation and page scripts are
 * otherwise left byte-for-byte as supplied by the export.
 */
export function landingTemplateHtml(proof: Result<DemoProof>): string {
  const withoutReferralTracking = templateCode.replace(
    /<script>\s*try\{if\(window\.parent[\s\S]*?<\/script>\s*/i,
    "",
  );

  return replaceTemplateIcons(adaptTakegraphContent(withoutReferralTracking, proof));
}
