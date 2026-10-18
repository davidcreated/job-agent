# job-agent

**An assistant that finds software engineering jobs, checks whether you can
actually take them, and writes your applications for you.**

Open source, runs on your own computer, and works with a free AI key.

## What problem does it solve?

Job hunting is repetitive. You search many sites, read hundreds of postings,
discover halfway through that a role is closed to your country, then write the
same cover letter again. job-agent does the searching, the sorting and the
first draft. You keep the decision about what gets sent.

## What it does, in plain words

1. **Finds jobs.** It checks the career pages of hundreds of companies and
   several job feeds, around the clock.
2. **Throws out the obvious misses.** Wrong role, too junior, pays too little,
   a country you can't work in. This step is cheap and fast.
3. **Reads what is left like a recruiter would.** An AI scores how well each job
   fits you and, importantly, whether the company would really hire someone in
   your location. It quotes the sentence it based that on.
4. **Writes your application.** A cover letter and a CV reordered to match the
   job. The AI may only use facts you gave it, so it cannot invent experience.
5. **Sends it or asks you first.** By email or by filling in the company's web
   form. You choose between full automation and "ask me on Telegram first".

## Works anywhere in the world

Nothing is tied to one country. You tell it where you live and where you are
allowed to work, and it filters for you. Someone in Lagos, Berlin or São Paulo
uses the same program with different settings.

## Safe by default

- **Nothing is sent until you turn sending on.** It starts in a practice mode.
- A daily cap on applications, and a kill switch: create a file named `data/STOP`.
- It never applies to the same job twice.
- Scraped sites like LinkedIn and Indeed are never auto-applied unless you
  explicitly allow it, because their rules forbid automation.

## What you need

- A computer that stays on (a Mac or Linux machine, or a small server).
- An API key for any one of 15 AI providers: Gemini (has a free tier), Claude, GPT, Grok, DeepSeek, Mistral, Qwen, Kimi, GLM, MiniMax, Cohere, Llama (Groq or Together), OpenRouter, or a free local model through Ollama.
- Optional: a Gmail account to send from, and a Telegram bot to approve jobs
  from your phone.
- Python 3.10 or newer.

## Getting started

```bash
git clone https://github.com/davidcreated/job-agent.git
cd job-agent
python3 -m venv .venv
.venv/bin/pip install requests pyyaml feedparser python-jobspy reportlab playwright

cp .env.example .env                                  # add your keys
cp config/profile.example.yaml config/profile.yaml    # about you
cp config/cv.example.yaml config/cv.yaml              # your CV, as data
```

Then open three files and make them yours:

| File | What you decide |
|---|---|
| `config/profile.yaml` | Who you are, where you live, where you may work, what the AI may claim |
| `config/cv.yaml` | Your CV in structured form |
| `config/criteria.yaml` | Which roles, stacks, regions and salaries you want |

Choose your AI in `config/criteria.yaml` (`scoring.provider`) and add its key to `.env`.
`python scripts/llm.py` lists all 15 and shows which keys you have set.

Run it:

```bash
.venv/bin/python scripts/db.py                # create the local database
.venv/bin/python scripts/discover_boards.py   # find company career pages
.venv/bin/python scripts/fetch_ats.py         # collect jobs
.venv/bin/python scripts/score.py             # let the AI rate them
.venv/bin/python scripts/status.py            # see where things stand
```

To keep it running in the background, use `scripts/watch.py`. On a Mac, the
`ops-launchd*.plist` files are templates for that.

## Your data stays yours

Your profile, CV, keys and job history live in files on your machine and are
excluded from git. Nothing is uploaded except the job text and your profile
facts sent to the AI service you configure.

## Going deeper

[ARCHITECTURE.md](ARCHITECTURE.md) explains the pipeline, the safety model and
the bugs that shaped the design. Run `.venv/bin/python tests/test_filters.py`
before changing any filter.

## License

MIT. See [LICENSE](LICENSE).
