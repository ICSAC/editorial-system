"""ICSAC Editorial System — Configuration.

Copy this file to config.py. Secrets are loaded from environment variables.
Set them in /etc/icsac/editorial.env (loaded by systemd EnvironmentFile=).

For manual runs: source /etc/icsac/editorial.env && export ZENODO_TOKEN TELEGRAM_TOKEN TELEGRAM_CHAT_ID
"""

import os

import os as _os


def _load_env_file(path: str = "/etc/icsac/editorial.env") -> None:
    """Self-load env file if vars not already set. Lets Python invocations
    work without ceremony — systemd EnvironmentFile= still wins when present.
    """
    p = _os.path.expanduser(path)
    if not _os.path.isfile(p):
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            _os.environ.setdefault(k, v)


_load_env_file()


ZENODO_TOKEN = os.environ.get("ZENODO_TOKEN", "")
ZENODO_API = "https://zenodo.org/api"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Optional: Telegram supergroup-thread routing. When set, editorial-system
# messages pin to a specific topic so notifications can share a bot/supergroup
# with other operator monitoring without crossing streams.
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_ID", "")

# Optional: Tier-3 test routing. The submission worker (when test_mode and
# tier=3) and apply_decision (when applying a verdict to a test sub_id)
# route curator-facing Telegram to this chat instead of TELEGRAM_CHAT_ID.
# Leave unset to skip the curator Telegram in T3 entirely (panel/RQC/email
# draft still run); the worker logs a single warning when this happens.
TELEGRAM_TEST_CHAT_ID = os.environ.get("TELEGRAM_TEST_CHAT_ID", "")
TELEGRAM_TEST_THREAD_ID = os.environ.get("TELEGRAM_TEST_THREAD_ID", "")

# Optional: IMAP draft-save mode. When email_send is invoked with draft=True,
# the rendered MIME message is APPENDed to Gmail's Drafts folder via IMAP
# (operator manually reviews + sends from Gmail UI). Leave unset to disable
# draft mode entirely.
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_DRAFTS_FOLDER = os.environ.get("IMAP_DRAFTS_FOLDER", "[Gmail]/Drafts")


OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
# Panel slot chains. Entries are tagged with backend prefix:
#   "hf|<model>:<provider>"  → HuggingFace Inference Providers Router
#                              (custom provider keys live in HF settings;
#                               billing routes through the upstream provider)
#   "or|<model>"             → OpenRouter direct
# Untagged entries fall through to OR for backward compatibility.
# Consecutive OR entries are batched into a single OR call (`models` array,
# max 3 per OR's cap). HF entries fire one HTTP request each because HF's
# explicit provider pin does not auto-failover within the call — the panel
# chain dispatcher is responsible for trying the next entry on failure.
#
# 2026-09-27: Groq retired Llama-3.3-70B (404), Cerebras retired Qwen3-235B
# (410), and Groq's free tier caps gpt-oss-120b at 8k tokens/min -- a paper is
# ~10k tokens, so it 413s on every real submission. Every slot therefore fell
# to its OR :free tail, which was rate-limited upstream (429), and a submission
# paused twice. Primaries now pin deepinfra (paid from HF credits, ~1.5c per
# paper at list price; all four verified 200 at full paper size 2026-09-27).
# One model family per slot: DeepSeek / OpenAI open-weights / Qwen / Google.
# Trade-off: one paid provider, so a deepinfra outage drops every slot to its
# OR :free tail at once. No other provider is enabled on the HF account.
# OR :free tails pruned 2026-09-27 to what OR's catalog lists (17 models):
# gpt-oss-120b, glm-4.5-air, nemotron-nano-12b, hermes-3-405b and minimax-m2.5
# :free are gone -- the new model check flagged them. Live cross-family picks
# with paper-sized context: qwen3.8-27b (untested in the panel; last resort)
# and the two gemma-4s. Slot 3's tail skips qwen since its primary is Qwen.
# Second entry per slot (2026-09-27): a cross-family deepinfra fallback. A
# reviewer output that fails the schema check is a MODEL-shaped failure; the
# retry used to re-walk the same primary, then fall to the OR :free tail, which
# 429'd all day -- two slots lost per pass, panel below MIN_REVIEWERS. A
# different model on the same provider absorbs it. Same trade-off as 04-27:
# a fallback can duplicate another slot's primary; reliability beats diversity.
OPENROUTER_MODELS = [
    # 2026-09-28 (the curator): a free direct provider first wherever one exists, the
    # HF router second (a $0.10/month credit pool), OpenRouter :free last (a shared
    # pool that 429s). `oai|<provider>|<model>` entries need the provider's key in
    # the environment (OAI_COMPAT_PROVIDERS); without it they are skipped silently,
    # so the roster can be wired before the keys exist. A paper the panel cannot
    # staff is re-queued every batch tick (panel_retry.py) and screams after 24 h.
    # Slot 1: DeepSeek family.
    [
        "oai|sambanova|DeepSeek-V3.1",
        "hf|deepseek-ai/DeepSeek-V4-Flash:deepinfra",
        "hf|zai-org/GLM-4.7-Flash:deepinfra",
        "or|nvidia/nemotron-3-ultra-550b-a55b:free",
        "or|qwen/qwen3.8-27b:free",
        "or|google/gemma-4-31b-it:free",
    ],
    # Slot 2: OpenAI open-weights.
    [
        "oai|sambanova|gpt-oss-120b",
        "hf|openai/gpt-oss-120b:deepinfra",
        "hf|Qwen/Qwen3-235B-A22B-Instruct-2507:deepinfra",
        "or|nvidia/nemotron-3-super-120b-a12b:free",
        "or|qwen/qwen3.8-27b:free",
        "or|google/gemma-4-26b-a4b-it:free",
    ],
    # Slot 3: Mistral / Qwen family (Mistral activates when MISTRAL_API_KEY exists).
    [
        "oai|mistral|mistral-large-latest",
        "hf|Qwen/Qwen3-235B-A22B-Instruct-2507:deepinfra",
        "hf|google/gemma-4-31B-it:deepinfra",
        "or|nvidia/nemotron-3-ultra-550b-a55b:free",
        "or|google/gemma-4-26b-a4b-it:free",
        "or|google/gemma-4-31b-it:free",
    ],
    # Slot 4: Google family.
    [
        "hf|google/gemma-4-31B-it:deepinfra",
        "or|google/gemma-4-31b-it:free",
        "oai|sambanova|gemma-4-31B-it",
        "or|nvidia/nemotron-3-super-120b-a12b:free",
        "or|qwen/qwen3.8-27b:free",
        "or|google/gemma-4-26b-a4b-it:free",
    ],
]
OPENROUTER_MODELS_API_URL = "https://openrouter.ai/api/v1/models"

# OpenAI-compatible direct providers for `oai|<provider>|<model>` panel entries
# (2026-09-28). Keys come from the environment; an entry whose key is absent is
# skipped, not counted as a dead chain link.
OAI_COMPAT_PROVIDERS = {
    # free tier, no card: about 20 requests and 200K tokens per day, 128k context
    "sambanova": {"base_url": "https://api.sambanova.ai/v1", "key_env": "SAMBANOVA_API_KEY"},
    # free "experiment" mode; its limits are shown only in the Mistral admin panel
    "mistral":   {"base_url": "https://api.mistral.ai/v1",   "key_env": "MISTRAL_API_KEY"},
}
# panel_retry.py (batch-tick step 2b): a paper the panel could not staff is
# re-queued every tick and SCREAMS (Telegram + pain) after this many hours of
# waiting for reviewers, counted from its first pause, never from receipt. A
# paper stuck in_review this long with no result is treated as a dead worker.
PANEL_SCREAM_HOURS = float(os.environ.get("ICSAC_PANEL_SCREAM_HOURS", "24"))
PANEL_STUCK_HOURS = float(os.environ.get("ICSAC_PANEL_STUCK_HOURS", "3"))
# after this many automatic re-queues (about five days at two ticks a day) the
# paper keeps screaming but is not re-run again without a human
PANEL_MAX_RETRIES = int(os.environ.get("ICSAC_PANEL_MAX_RETRIES", "10"))
# papers paused before this instant are announced once and left parked
PANEL_RETRY_SINCE = os.environ.get("ICSAC_PANEL_RETRY_SINCE", "2026-09-28T00:00:00Z")

# ── DOI registrar ─────────────────────────────────────────────────────────────
# "crossref": accept stages a Crossref deposit draft (crossref_deposit.stage) --
#             nothing is minted until an operator runs intake/register-doi.sh
#             --live. ICSAC has been a Crossref member since 2026-07-24.
# "zenodo":   the pre-membership path (repository_deposit) -- a Zenodo draft
#             that mints 10.5281/zenodo.* on publish. Kept for the backfile.
DOI_REGISTRAR = os.environ.get("DOI_REGISTRAR", "crossref")
CROSSREF_PREFIX = os.environ.get("CROSSREF_PREFIX", "10.67697")
# The "Last revised" date shown on icsacinstitute.org/terms. Written into every
# submission record so we can say which Terms an author accepted. Keep in sync.
TERMS_VERSION = os.environ.get("ICSAC_TERMS_VERSION", "2026-09-27")
# Author objection window (days) opened at accept; the acceptance email carries
# a personal /approve/ link and this deadline. register --live waits for an
# approval or the window's close unless the operator overrides.
OBJECTION_WINDOW_DAYS = int(os.environ.get("ICSAC_OBJECTION_WINDOW_DAYS", "7"))
# Days after DOI registration before the ONE post-publication follow-up (the
# reviewer invitation) is DRAFTED for the curation team. Never sent by code;
# never repeated; skipped when the author excluded newsletters, held, withdrew,
# or is already on the website registries. See intake/post_publication.py.
FOLLOWUP_DAYS = int(os.environ.get("ICSAC_FOLLOWUP_DAYS", "7"))

SITE_BASE_URL = "https://icsacinstitute.org"
# Suffix pattern; fields: {year} {seq} {sub_id}. seq is per-year, persisted in
# CROSSREF_SEQ_FILE. Drafts can be re-staged under a new pattern for free;
# a registered DOI cannot be changed.
# The suffix is the Institute's, not the journal's: the journal lives in the
# deposit metadata (journal_metadata + the volume block below), so the DOI
# string never changes if the serial is ever renamed or split.
CROSSREF_DOI_SUFFIX = os.environ.get("CROSSREF_DOI_SUFFIX", "icsac.{year}.{seq:03d}")
CROSSREF_SEQ_FILE = os.environ.get("CROSSREF_SEQ_FILE",
                                   os.path.expanduser("~/icsac-submissions/.doi-seq"))
# "journal-article" (DEFAULT since 2026-09-28): Persistence is the journal of
#   record, published online at icsacinstitute.org. Every accepted paper is an
#   article of the open volume from the day its DOI registers; the paperback and
#   ebook are that volume's annual edition (print inclusion is not guaranteed;
#   policy at /journal#how-it-works). The print date and pages are added later by
#   redepositing the same DOI. OpenAlex reads "article" once an ISSN is attached.
# "report-paper": a paper the Institute publishes outside the journal
#   (explicit <publisher> = ICSAC; OpenAlex type "report").
# "posted_content": Crossref's preprint class (OpenAlex labels it "preprint").
CROSSREF_CONTENT_TYPE = os.environ.get("CROSSREF_CONTENT_TYPE", "journal-article")
CROSSREF_JOURNAL_TITLE = "Persistence"
CROSSREF_JOURNAL_ABBREV = ""            # none registered yet
CROSSREF_JOURNAL_ISSN = os.environ.get("CROSSREF_JOURNAL_ISSN", "")   # eISSN pending (LoC refile >= 2026-11-12)
# The open volume: everything accepted until a volume's print cutoff is deposited
# as an article of it. Bump BOTH by hand when the next volume opens (Volume 1
# closes with the May 2027 edition). The year is the volume's online year: the
# schema requires a date on the volume block; the print date comes at redeposit.
# An empty CROSSREF_JOURNAL_VOLUME omits the volume block entirely.
CROSSREF_JOURNAL_VOLUME = os.environ.get("CROSSREF_JOURNAL_VOLUME", "1")
CROSSREF_JOURNAL_VOLUME_YEAR = os.environ.get("CROSSREF_JOURNAL_VOLUME_YEAR", "2026")
CROSSREF_POSTED_TYPE = "other"          # only used for posted_content
CROSSREF_DEPOSITOR_NAME = "ICSAC"
CROSSREF_DEPOSITOR_EMAIL = "help@icsacinstitute.org"
CROSSREF_REGISTRANT = "Institute for Complexity Science and Advanced Computing"
CROSSREF_PUBLISHER_NAME = CROSSREF_REGISTRANT
CROSSREF_PUBLISHER_PLACE = "Fort Wayne, Indiana, USA"
# Same-origin PDF the deposit points Similarity Check / text-mining at.
# register --live copies paper.pdf to <website>/public/papers/<sub_id>.pdf.
CROSSREF_PDF_URL = "https://icsacinstitute.org/papers/{sub_id}.pdf"
CROSSREF_SCHEMA_DIR = os.environ.get("CROSSREF_SCHEMA_DIR",
                                     os.path.expanduser("~/Desktop/icsac/crossref-schemas"))
# Deposit credentials: the technical contact's personal Crossref login + role.
# Live in ~/.config/crossref.env (loaded by intake/register-doi.sh), never here.
CROSSREF_LOGIN_EMAIL = os.environ.get("CROSSREF_LOGIN_EMAIL", "")
CROSSREF_ROLE = os.environ.get("CROSSREF_ROLE", "")
CROSSREF_PASSWORD = os.environ.get("CROSSREF_PASSWORD", "")

# ── CiteStamp citation-graph check (Phase 3 of citation verification) ────────
# Public MCP endpoint, stateless JSON-RPC, no token needed; a token raises the
# anonymous rate ceiling if one is ever issued to the pipeline (env only).
CITESTAMP_MCP_URL = os.environ.get("CITESTAMP_MCP_URL", "https://mcp.citestamp.com/mcp")
CITESTAMP_MCP_TOKEN = os.environ.get("CITESTAMP_MCP_TOKEN", "")

# Self-heal thresholds (claude + 4 OR slots = 5 total panelists per pass).
# MIN_REVIEWERS=4 tolerates 1 slot failure per pass after self-heal retry.
# Combined with REVIEW_PASSES below, a paper yields 8-10 valid reviews in
# the aggregate. Tightened from MIN_REVIEWERS=3 + 3 passes 2026-04-26
# after observing pass-to-pass stdev was uniformly tiny (≤0.41 on the
# noisiest dim, ≤0.09 on most) — 3rd pass added marginal stderr at 33%
# more compute. Two passes captures essentially the same signal; pairing
# with MIN_REVIEWERS=4 keeps each pass closer to full panel.
MIN_REVIEWERS = 4
MAX_SLOT_RETRIES = 1           # per failed slot, after the initial attempt
RETRY_COOLDOWN_SEC = 30        # wait between initial pass and retry pass

# Multi-pass aggregation: run the full panel N times, aggregate mean+stdev
# across passes. 2 passes balances stability against compute. Set to 1
# to disable multi-pass; 3+ for noise-reduction at compute cost.
REVIEW_PASSES = 2

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "info@icsacinstitute.org")
REPLY_TO_EMAIL = os.environ.get("REPLY_TO_EMAIL", "info@icsacinstitute.org")

NTFY_PAIN_URL = os.environ.get("NTFY_PAIN_URL", "")
NTFY_BACKUPS_URL = os.environ.get("NTFY_BACKUPS_URL", "")
BRAIN_URL = os.environ.get("BRAIN_URL", "")
KUMA_PUSH_URL = os.environ.get("KUMA_PUSH_URL", "")

COMMUNITY_ID = "icsac"
GOOGLE_FORM_URL = os.environ.get("GOOGLE_FORM_URL", "https://example.com/your-community-signup-form")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Runtime output directory — created on first run, gitignored.
REVIEWS_DIR = os.path.join(BASE_DIR, "reviews")
# Runtime output directory — created on first run, gitignored.
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
RUBRICS_DIR = os.path.join(BASE_DIR, "rubrics")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Site base URL used to build share-target landing pages (icsacinstitute.org/accepted/<id>)
SITE_BASE_URL = "https://icsacinstitute.org"

CLAUDE_CMD = "claude"
# DEPRECATED (2026-05-22): unused. The gemini-cli free tier sunsets
# 2026-06-18. Blind-review compaction now uses CLAUDE_CMD, and the panel's
# Gemini-family voice is served via OpenRouter google/gemma :free models.
# No remaining code path invokes this binary. Kept only to avoid an
# AttributeError in any external fork that still references it.
GEMINI_CMD = "gemini"

RUBRIC_DIMENSIONS = [
    "domain_fit",
    "methodological_transparency",
    "internal_consistency",
    "citation_integrity",
    "novelty_signal",
    "ai_provenance_signal",
]

# Path to the institute's website repo (used by publications.py to commit
# accepted-paper landing pages + redacted reviews). Empty disables the
# website-registry push; the Zenodo accept itself still proceeds.
ICSAC_WEBSITE_REPO = os.environ.get("ICSAC_WEBSITE_REPO", "")
