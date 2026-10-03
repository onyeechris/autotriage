# The triage Lambda, explained for someone new to this codebase

Every file in this folder now has a comment on nearly every line, explaining what that specific
line does. This README is the companion piece: the things that are easier to explain once, up
here, than to repeat five times inline -- the big picture, the order things run in, and a
glossary of Python/AWS concepts that show up over and over in the comments.

If you're new to Python, read the **Concepts glossary** section first. Then read the five files
in the order listed in **Reading order**, since each one builds on ideas from the last.

---

> **Note:** this folder is an annotated *teaching copy*. It is not what's deployed. The deployed
> source is `lambda/triage/`, which has later fixes this copy doesn't: floats are converted to
> `Decimal` before the DynamoDB write, OK notifications are ignored, metrics are read oldest-first
> with timestamps, the disk query includes `fstype`, and the rate-limit window is configurable.
> See the "What changed" section of `aws_readme.md`.

## What this folder is, in one paragraph

This is the code for one AWS Lambda function. A Lambda function is just "a chunk of code AWS
runs for you, on demand, without you having to manage a server." AWS starts it up whenever an
event happens — here, whenever one of our three CloudWatch alarms fires and sends a message
through SNS. The Lambda's job, every time it runs, is: read what happened, ask Claude to
diagnose it, decide (very cautiously) whether it's safe to fix automatically, maybe fix it, check
whether the fix worked, and write down exactly what it did and why — every single time, whether
it acted or not.

## The five files, and why they're separate

Splitting one job into five files isn't strictly necessary — you could paste everything into one
giant file and it would still work. It's done here because each file has one clear
responsibility, which makes each one easier to read on its own, and easier to test in isolation:

| File | Job | Depends on AWS? |
|---|---|---|
| `prompts.py` | The exact text sent to Claude | No — pure text |
| `policy.py` | The safety rules: what's allowed to run automatically | No — pure logic |
| `actions.py` | Actually running a fix, via SSM | Yes |
| `context.py` | Fetching logs / metrics / the runbook | Yes |
| `handler.py` | Ties all four of the above together | Yes |

Notice that `prompts.py` and `policy.py` don't touch AWS at all — they're just text and logic.
That's deliberate: it means you could copy those two files out, run them on your own laptop with
plain Python, and test them without needing any AWS credentials at all. That's exactly what the
functional check further down in this project's test process does.

## Reading order

**1. `prompts.py` first.** It's the shortest file and touches no AWS services — just two chunks
of text. Understanding what we actually ask Claude for makes everything downstream make sense.

**2. `policy.py` second.** This is the file you'd point to if someone asked "how do you know this
won't do something dangerous automatically?" It has no AWS calls either — it's pure decision
logic, operating on plain Python dictionaries and booleans.

**3. `actions.py` third.** Now we start touching AWS: this file is the *only* place in the whole
project that can actually change something on the EC2 instance, via one specific AWS API call
(`ssm_client.send_command`).

**4. `context.py` fourth.** Three independent functions, each fetching one kind of "evidence"
(logs, metrics, the runbook) from a different AWS service, so the Lambda has something real to
show Claude.

**5. `handler.py` last.** The orchestrator. It imports functions from all four files above and
calls them in the right order. This is also the only file that contains `lambda_handler`, the one
specific function name AWS itself calls when the Lambda runs.

---

## Concepts glossary

These are things that show up repeatedly in the line-by-line comments across all five files.
Rather than re-explain them every time, here's each one, once, in plain terms.

### Basic Python

- **Module** — a fancy word for "a `.py` file." Each of the five files is one module.
- **`import`** — brings in code from somewhere else (Python's own built-in tools, or another file
  in this same folder) so you can use it. `import json` brings in the whole `json` module;
  `from policy import decide` brings in just the one `decide` function from `policy.py`.
- **Docstring** — a string written as the very first thing in a file or function, used purely as
  documentation (not printed or shown anywhere unless something specifically asks for it, like
  Python's `help()`).
- **Comment** — anything after a `#` on a line. Python completely ignores it; it exists purely
  for humans reading the code.
- **Variable** — a name that points to a value, e.g. `confidence = 0.9`. `ALL_CAPS` names are a
  convention (not a rule Python enforces) meaning "treat this as a constant, don't change it."
- **Function** (`def ...`) — a named, reusable block of code. Everything indented underneath the
  `def` line is "inside" that function. `return` sends a value back to whoever called it.
- **Type hints** (`name: str`, `-> bool`) — notes about what kind of value a parameter or return
  value *should* be. They're purely documentation for humans and tools (like an editor's
  autocomplete) — Python itself doesn't enforce them at all.
- **f-string** (`f"hello {name}"`) — a string with a lowercase `f` right before the opening quote.
  Anything inside `{curly braces}` is replaced with the real value of that expression.
- **`.format()` alternatives / `!r`** — inside an f-string, `{value!r}` shows the value the way
  Python itself would type it back (e.g. a string gets its quote marks shown), which is handy for
  debug messages.
- **`if` / `elif` / `else`** — branching: run one block of code or another, depending on whether a
  condition is true.
- **`for ... in ...`** — a loop: run a block of code once for every item in a list (or similar).
- **`while ...`** — a loop that keeps running as long as some condition stays true.
- **`try` / `except`** — "attempt this; if it raises an error, run this other code instead of
  crashing the whole program." Each `except SomeError:` only catches that specific kind of error.
- **`continue`** — inside a loop, skip the rest of this pass and jump straight to the next item.
- **`break`** — exit a loop immediately, even if it would otherwise have kept going.
- **Truthy / falsy** — in an `if` check, Python treats `None`, `0`, `""` (empty string), `[]`
  (empty list), and `{}` (empty dict) as "false-ish", and basically everything else as "true-ish."
  This is why you'll see things like `if missing:` (true only if the set isn't empty) or
  `x or default` (uses `default` only if `x` is falsy).

### Python's data structures

- **String** (`str`) — text, e.g. `"hello"`.
- **List** (`[1, 2, 3]`) — an ordered collection; can contain duplicates; you access items by
  position, e.g. `my_list[0]` for the first one.
- **Dictionary / dict** (`{"key": "value"}`) — a collection of `key: value` pairs. You look things
  up by key, e.g. `my_dict["key"]`. This is exactly what a parsed JSON object turns into in
  Python. `.get("key", default)` reads a key *safely*, returning `default` (instead of crashing)
  if the key doesn't exist.
- **Set** (`{"a", "b"}`) — like a list, but no duplicates and no order; great for "is X one of
  these allowed values?" checks. Note: `{}` alone means an *empty dict*, not an empty set — you'd
  write `set()` for that (not that it comes up in this codebase).
- **Tuple** (`("a", "b")`) — a small, fixed-size, ordered group of values. `return "auto", "why"`
  automatically creates and returns a two-item tuple.
- **Slicing** (`text[:500]`, `text[4:]`) — pulling out part of a string or list.
  `text[:500]` means "the first 500 characters"; `text[4:]` means "everything from index 4
  onward"; `text[-2000:]` means "the last 2000 characters."
- **Comprehensions** (`{f["field"]: f["value"] for f in row}`) — a compact way to build a new
  list, dict, or set by looping over something, all in one line, instead of writing a full `for`
  loop with `.append()` calls.

### AWS / cloud concepts used here

- **boto3** — the official AWS SDK (software development kit) for Python. `boto3.client("s3")`
  gives you an object with methods for every S3 API action; other AWS services work the same way.
- **Environment variables** — settings passed into a running program from *outside* it (here, set
  by Terraform when it creates the Lambda). Read in Python via `os.environ["NAME"]` (crashes if
  missing) or `os.environ.get("NAME", "default")` (falls back quietly).
- **Cold start / warm invocation** — the first time AWS runs your Lambda after a while, it has to
  set everything up from scratch (a "cold start"); if it gets called again soon after, AWS often
  reuses that same running environment (a "warm" invocation). Code sitting at the top level of a
  file (like our `boto3.client(...)` calls) only re-runs on a cold start, which is why it's a
  good place to put one-time setup work.
- **JSON** — a text format for structured data that looks almost exactly like a Python
  dict/list, and is the universal way AWS APIs, HTTP APIs, and this project's own logs represent
  data. `json.dumps(x)` turns a Python object into JSON text; `json.loads(text)` turns JSON text
  back into a Python object.
- **SNS (Simple Notification Service)** — AWS's pub/sub messaging service. One thing "publishes"
  a message to a "topic"; anything "subscribed" to that topic (a Lambda, an email address,
  etc.) receives it.
- **SSM (Systems Manager) Run Command** — lets you run a predefined script on an EC2 instance
  remotely, through AWS's API, without needing SSH access to it.
- **SSM Parameter Store** — a simple key/value storage service, often used for configuration and
  secrets. A "SecureString" parameter is encrypted at rest; reading one back in plaintext
  requires explicitly asking for decryption (`WithDecryption=True`).
- **CloudWatch Logs / Logs Insights** — where the app's log lines end up, and a query language
  for searching through them.
- **CloudWatch Metrics / Alarms** — numeric time-series data (like CPU usage), and rules that
  watch that data and change state (OK / ALARM) when a threshold is crossed.
- **DynamoDB** — AWS's NoSQL database. A "table" here is a collection of items (rows), each
  identified by a partition key (and, when the table has one, a sort key) rather than needing
  a fixed set of columns the way a SQL table does.
- **S3** — AWS's file storage service. Files are called "objects," identified by a "key" (their
  path/name inside a "bucket").

---

## A worked example: one line from `handler.py`, fully unpacked

To show how the comment style in these files works, here's one real line, in context:

```python
lines.append(f"  - {item}")
```

Reading the comment that follows it in `handler.py`:

> `.append()` adds one new item onto the end of the existing "lines" list, in place.

Breaking that down further, for a total beginner:
- `lines` is a **list** — created earlier in the same function as `lines = [...]`.
- `.append(...)` is a **method** (a function that belongs to an object) — every Python list has
  one, and calling it adds one new item onto the end of that specific list.
- `f"  - {item}"` is an **f-string**: it builds the text `"  - "` followed by whatever `item`
  currently is, on this pass of the loop.
- Put together, this line runs once per item in a loop, and after it runs, `lines` has one more
  entry in it than it did before.

That's the level of detail behind essentially every comment in these five files — if a line still
doesn't make sense after reading its comment, it's very likely explained by one of the concepts
in the glossary above.

---

## How this maps back to the rest of the project

This README (and the inline comments) explain *how the Python works, line by line*. For *why*
this Lambda exists, how it fits into the alarm → SNS → Lambda → remediation pipeline, and how it
relates to the Terraform, the SSM documents, and the runbook — see `README_exp.md` in the project
root, particularly its "How a single incident flows through the system" section and its own
(higher-level) walkthrough of these same five files.
