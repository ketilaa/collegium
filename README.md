# Collegium

Collegium is a persistent research organization of AI agents with
institutional memory. Its roles (Scout, Researcher, Skeptic, Strategist and
Historian) run on one local language model, read the web, and build
structured, explainable knowledge in PostgreSQL: observations, evidence,
hypotheses, and how confidence in them changes over time. A human owner
steers it as board chair.

The code is not published here yet.

## If Collegium visited your site

Collegium identifies itself in the User-Agent of every request it makes:

```
Collegium/0.1 (+https://github.com/ketilaa/collegium; research agent; operated by <contact>)
```

- **This repository is the software, not the operator.** Anyone can run their
  own copy of Collegium, and the authors do not run, control or see other
  people's copies.
- **"operated by"** names whoever runs the copy that visited you. Contact
  them about its behaviour. A copy without that part has not told us who
  runs it.
- **robots.txt is respected.** To keep Collegium out of all or part of your
  site:

  ```
  User-agent: Collegium
  Disallow: /
  ```

- **It reads; it does not write.** It fetches pages and feeds it has found
  through search, at a modest pace, and never posts, submits forms or signs
  up for anything. The one exception is the forum Moltbook, where a copy's
  community agent may post and reply, each post approved by its operator.

## Running your own copy

When the code is published: set `COLLEGIUM_CONTACT` to a URL or `mailto:`
address of yours, so that sites can tell who is reading them. Collegium
warns at startup when it is not set.
