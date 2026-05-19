import sys
from gtts import gTTS

def normalize(text):
    return (text
        .replace("‘", "'").replace("’", "'")   # curly single quotes
        .replace("“", '"').replace("”", '"')   # curly double quotes
        .replace("–", "-").replace("—", "-")   # en/em dash
        .replace("\xa0", " ")                            # non-breaking space
    )

text, filepath = sys.argv[1], sys.argv[2]
gTTS(text=normalize(text), lang="en", slow=False).save(filepath)
