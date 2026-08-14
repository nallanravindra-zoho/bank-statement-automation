"""
gemini_client.py -- minimal Gemini client for this service, using the OLDER
`google.generativeai` SDK (import `google.generativeai as genai`) -- per
Ravindra's explicit instruction: "use the depricated api (SDK) only
googlegenerativeai for now ... and implement this in zoho use case".

2026-08-12 -- THIRD access-method change this same day, all live-tested
before landing here:
  1. Raw REST (this file's original approach) broke live: "gemini-flash-
     latest" was silently moved by Google to a newer model generation that
     no longer accepts the REST generationConfig.thinkingConfig.
     thinkingBudget field this code used to send (which worked fine,
     earlier in this same project). A follow-up guess
     (generationConfig.thinkingLevel) ALSO failed live, with Google's own
     error confirming that field isn't recognized either, on this REST
     endpoint/version: `"Unknown name \\"thinkingLevel\\" at
     'generation_config': Cannot find field."`
  2. `google-genai` (Google's current, actively-supported SDK) was tried
     next, per Ravindra's "REST one is not reliable use the google genai
     one only" -- verified against the actually-installed package
     (client construction, config fields, response shape all confirmed via
     introspection, not guessed) and wired in cleanly.
  3. Ravindra then asked to go back to THIS package -- `google.generativeai`
     -- explicitly ("as suggested by you"): this is the one approach that
     had ALREADY been confirmed working with real, live, un-truncated
     correct output earlier this same day, in the standalone
     test_gemini_narration_sdk.py script (same model, same short
     narration-classification task, no thinking-config knob needed at all).
     `google-genai` had NOT yet been confirmed working with a real key by
     the time this decision was made.

KNOWN TRADEOFF, explicitly accepted per Ravindra's own instruction, not
overlooked: `google.generativeai` prints its own deprecation notice on
import ("All support ... has ended ... no longer receiving updates or bug
fixes") -- confirmed this fires ONCE per process (a module-level
warnings.warn() in the package's own __init__.py), not once per request, so
on Cloud Run this shows up once in the logs per cold start/instance, not as
per-request noise. If Google ever removes the package from PyPI entirely
(not just stops updating it), this file would need to move to google-genai
or REST again at that point -- worth revisiting periodically, not just once.

THINKING CONTROL -- same reasoning as the other two approaches tried today:
this SDK has NO thinking_config/thinking_budget field at all, on any
version (confirmed via this package's own GenerationConfig source/docs) --
so there's nothing to configure here, and no risk of guessing a wrong field
name for it either. Relies on a generous max_output_tokens (1024) as the
defense against thinking tokens starving the visible answer -- same
approach already confirmed working live in test_gemini_narration_sdk.py.

MODEL NAME -- same "gemini-flash-latest" rolling alias as before -- override
via GEMINI_MODEL if a pinned model is ever wanted instead. NOT dynamically
selected via genai.list_models() -- that was tried and burned twice in the
standalone test script (list_models() listed models that then 404'd as
unavailable) before landing on this hardcoded default.

genai.configure() QUIRK, worth knowing: this SDK's API key is set via a
MODULE-LEVEL global call (genai.configure(api_key=...)), not per-client-
instance the way google-genai's genai.Client(api_key=...) works -- calling
it again with a different key anywhere in the same process would silently
change the key for every GeminiClient instance already constructed. Not a
practical problem for how this project uses it (one GEMINI_API_KEY per
deployment, read fresh from the environment on every _build_gemini_client()
call in main.py), but worth knowing if this client is ever reused in a
context with multiple different API keys in the same process.

TRANSPORT -- forced to "rest" explicitly (genai.configure(..., transport=
"rest")), NOT this SDK's default (grpc). Found live, while verifying this
file against the real installed package: with the default grpc transport,
a failing call (tested with a deliberately invalid API key) HUNG
indefinitely -- didn't even respect an explicit request_options={"timeout":
10} -- instead of failing fast the way every other client in this project
does on a bad request. Switching to transport="rest" made the exact same
failing call return in well under a second, with a clear error. This
matters a lot for this specific feature: generate_ai_reference_preview()
runs inside main.py's /post-transactions streaming loop, and this whole
feature's #1 design rule (see posting_service.py's module docstring) is
that an optional AI preview must NEVER be able to stall or take down a
real posting run -- a transport that can hang past its own configured
timeout is a direct threat to that guarantee, REST is not.

TIMEOUT -- generate_text() now takes an explicit `timeout` (seconds,
default 30, matching this project's other HTTP clients' own default),
passed as request_options={"timeout": timeout} on every call -- the old
REST-based version of this file always had an explicit timeout too; this
restores that same protection under the new SDK.

DEPENDENCY: requires `google-generativeai==0.8.6` (the last version ever
published -- confirmed via `pip install google-generativeai==` listing
available versions) -- requirements.txt updated accordingly, google-genai
removed from it (installing both isn't necessary and dragged in a
conflicting protobuf version locally when both were present).
"""
import os
from dataclasses import dataclass
from typing import List, Optional

import google.generativeai as genai

DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


@dataclass
class GeminiConfig:
    api_key: str
    model: str = DEFAULT_GEMINI_MODEL

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "Missing GEMINI_API_KEY in environment -- get one from https://aistudio.google.com/apikey "
                "(a Google AI Studio API key, NOT a Zoho or Google Cloud service-account credential)."
            )
        return cls(api_key=api_key, model=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL))


class GeminiClient:
    def __init__(self, config: GeminiConfig):
        self.config = config
        # Module-level global, not per-instance -- see module docstring's
        # "genai.configure() QUIRK" note above. transport="rest" -- see
        # module docstring's "TRANSPORT" note for why the default (grpc)
        # is deliberately avoided here.
        genai.configure(api_key=config.api_key, transport="rest")

    def generate_text(self, prompt: str, max_output_tokens: int = 1024, temperature: float = 0.2,
                       timeout: int = 30, stop_sequences: Optional[List[str]] = None) -> str:
        """Sends `prompt` as a single-turn request to Gemini via
        google.generativeai's GenerativeModel.generate_content() and
        returns the model's plain text reply, stripped.

        Deliberately scoped to plain-text-in/plain-text-out only -- no
        response_mime_type/response_schema/thinking-control knobs. This
        client used to also support forcing structured JSON output for the
        AI Reference Preview feature's earlier, more complex classification
        prompt -- that whole design was replaced 2026-08-12 with a much
        simpler narration-in/narration-out prompt (see posting_service.
        generate_ai_reference_preview()'s docstring), which no longer needs
        JSON mode at all, and this SDK's response_schema support was never
        exercised here as a result.

        stop_sequences (2026-08-12, added after a real live case: Ravindra's
        widget showed a row's AI Reference Preview as visible reasoning/
        commentary text -- "Let's check length: `...` = 34 characters (well
        under 80 chars). 3. **Refine Output:** * `Cyber" -- cut off mid-
        sentence, instead of the plain one-line answer the prompt asks for.
        This SDK has no way to disable Gemini's internal "thinking" (see
        module docstring's THINKING CONTROL note) and this model apparently
        sometimes narrates that reasoning as ordinary visible text rather
        than a separate hidden channel. A `stop_sequences` list is a real,
        long-standing generateContent field (confirmed via this package's
        own GenerationConfig fields, unlike the REST thinkingLevel field
        that turned out not to exist) -- passing ["\\n"] here means Gemini
        stops generating at the first newline, so even if it starts down a
        multi-line reasoning path, only whatever it wrote on the first line
        reaches this function. This alone doesn't guarantee the first line
        is clean prose rather than the start of a reasoning trace -- see
        posting_service.generate_ai_reference_preview()'s own additional
        cleanup/sanity check for the rest of this defense-in-depth fix.
        Optional/None by default so any other future caller of this generic
        client isn't forced into single-line output.

        Raises a RuntimeError on any SDK-level failure (network error,
        invalid API key, Gemini API error, ...) -- the API key is redacted
        from the message if it happens to appear in it (defensive, same
        redaction this project's other Gemini clients apply -- see this
        project's REST-based gemini_client.py history for why that
        precaution exists at all: a real API key leak happened once, via a
        DIFFERENT client's default error message). Also raises ValueError
        if the response has no usable text (e.g. blocked by safety
        filters).

        Never swallows an error itself -- see posting_service.
        generate_ai_reference_preview()'s docstring for why the CALLER is
        responsible for catching this and degrading gracefully, same
        "never let an optional/preview feature take down a real posting
        run" spirit as this project's other soft-fail helpers (e.g.
        posting_service._check_duplicate_description())."""
        generation_config = {"temperature": temperature, "max_output_tokens": max_output_tokens}
        if stop_sequences:
            generation_config["stop_sequences"] = stop_sequences
        model = genai.GenerativeModel(model_name=self.config.model, generation_config=generation_config)
        try:
            response = model.generate_content(prompt, request_options={"timeout": timeout})
        except Exception as e:
            msg = str(e)
            if self.config.api_key in msg:
                msg = msg.replace(self.config.api_key, "<redacted>")
            raise RuntimeError(
                f"error calling {self.config.model}:generateContent via google.generativeai -- {msg}"
            ) from e
        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise ValueError(f"Gemini returned no usable text -- raw response: {response!r}")
        return text

    # NOTE (2026-08-12): this class briefly also had an extract_json_from_pdf()
    # method (multimodal PDF-plus-prompt call, for a Supporting Document
    # VERIFICATION feature that read invoice PDFs via Gemini and checked
    # them against statement rows). That whole feature was explicitly
    # discarded by Ravindra the same day it was built ("i want to park the
    # document extraction and validation aside ... discard all the previous
    # changes made for validation and gemini pdf doc reading"), in favor of
    # a much simpler attach-only feature (main.py's /attach-supporting-docs)
    # that never calls Gemini at all -- so extract_json_from_pdf() was
    # removed along with it. generate_text() above is this project's only
    # remaining Gemini call.