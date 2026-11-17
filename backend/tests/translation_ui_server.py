"""Real isolated API; translation only uses a synthetic model, never a key."""
import sys
from app.main import app
from app.translate import translator, parallel, translation_run
from app.llm import LLMResponse


class SyntheticTranslator:
    def chat(self, messages, **kwargs):
        return LLMResponse(content="Synthetic translation: " + messages[-1]["content"])


translator.get_llm = lambda: SyntheticTranslator()
parallel.get_llm = lambda: SyntheticTranslator()
translation_run.get_llm = lambda: SyntheticTranslator()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
