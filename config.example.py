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
# Cross-provider redundancy (2026-04-27): every slot's chain spans Groq +
# Cerebras + OR-free so a single-provider outage can't take more than one
# chain entry per slot. Cerebras free-tier 8K context cap forces
# Qwen3-235B-A22B-Instruct-2507 (the 64K-context exempt model) anywhere
# Cerebras appears in a slot.
OPENROUTER_MODELS = [
    # Slot 1: Groq Llama-3.3-70B → Cerebras Qwen3-235B → OR cross-family.
    [
        "hf|meta-llama/Llama-3.3-70B-Instruct:groq",
        "hf|Qwen/Qwen3-235B-A22B-Instruct-2507:cerebras",
        "or|openai/gpt-oss-120b:free",
        "or|z-ai/glm-4.5-air:free",
    ],
    # Slot 2: Groq gpt-oss-120b → Cerebras Qwen3-235B → OR Nvidia/Hermes.
    # nemotron-3-super-120b-a12b excluded (won't emit JSON reliably).
    [
        "hf|openai/gpt-oss-120b:groq",
        "hf|Qwen/Qwen3-235B-A22B-Instruct-2507:cerebras",
        "or|nvidia/nemotron-nano-12b-v2-vl:free",
        "or|nousresearch/hermes-3-llama-3.1-405b:free",
    ],
    # Slot 3: Cerebras primary → Groq Llama → OR Google/cross-family.
    [
        "hf|Qwen/Qwen3-235B-A22B-Instruct-2507:cerebras",
        "hf|meta-llama/Llama-3.3-70B-Instruct:groq",
        "or|google/gemma-4-26b-a4b-it:free",
        "or|z-ai/glm-4.5-air:free",
    ],
    # Slot 4: HF Groq primary, HF Cerebras fallback, OR tail. Reordered
    # 2026-04-27 after qwen3-next-80b-a3b-instruct:free failed all 4
    # consecutive panel passes (SUB-00003 pass 0+1, SUB-00004 pass 0+1).
    # Kept minimax + gemma-4-31b as the OR tail so slot 4 still has a
    # full OR-only fallback path with model-family diversity from slots
    # 1-3 OR tails (gpt-oss/z-ai, nemotron/hermes, gemma-4-26b/z-ai).
    # NB: this puts slot 4 on the same primary (HF Groq llama-3.3) as
    # slot 1 — accepted trade-off; total Groq-outage now drops the panel
    # to 4/5 via Cerebras fallback rather than staying functional, but a
    # CHRONIC slot-4 failure (which is what we had) was permanently below
    # MIN_REVIEWERS=4 in pass 1. Reliability beats slot-level diversity.
    [
        "hf|meta-llama/Llama-3.3-70B-Instruct:groq",
        "hf|Qwen/Qwen3-235B-A22B-Instruct-2507:cerebras",
        "or|minimax/minimax-m2.5:free",
        "or|google/gemma-4-31b-it:free",
    ],
]
OPENROUTER_MODELS_API_URL = "https://openrouter.ai/api/v1/models"

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
