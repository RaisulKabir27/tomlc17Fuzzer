import os
from google import genai


MODEL = "gemini-3.1-flash-lite"


class GeminiClient:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY environment variable is not set."
            )

        self.client = genai.Client(api_key=api_key)

    def generate(self, prompt):
        interaction = self.client.interactions.create(
            model=MODEL,
            input=prompt,
            generation_config={
                "thinking_level": "high",
            },
        )

        usage = interaction.usage

        usage_info = {
            "input_tokens": getattr(
                usage, "total_input_tokens", None
            ),
            "output_tokens": getattr(
                usage, "total_output_tokens", None
            ),
            "thought_tokens": getattr(
                usage, "total_thought_tokens", None
            ),
            "total_tokens": getattr(
                usage, "total_tokens", None
            ),
        }

        return {
            "text": interaction.output_text,
            "usage": usage_info,
            "interaction_id": interaction.id,
        }