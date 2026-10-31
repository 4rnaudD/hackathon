from dotenv import load_dotenv; load_dotenv()
import anthropic
print(anthropic.Anthropic().messages.create(
    model="claude-sonnet-5-5", max_tokens=50,
    messages=[{"role": "user", "content": "ping"}]
).content[0].text)
