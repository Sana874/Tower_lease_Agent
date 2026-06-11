# Tower Lease Vetting Agent

This reads a lease request written in plain English (like "Du wants a 15kg antenna at 40m on TWR-101"), checks it against the tower's capacity and the rules for that region, and decides: approved, rejected, or send it to a human.

## Running it

You just need Python 3. Nothing to install for the normal mode.

    python tower_agent.py            -> runs my test cases (7 of them)
    python tower_agent.py --batch    -> runs a queue of requests + shows the impact numbers
    python tower_agent.py "Du wants a 15kg antenna at 40 meters on TWR-101."

Keep tower_agent.py, towers_inventory.json and regional_policies.txt in the same folder.

## How I built it

The flow is: text -> pull out the details -> look up the tower + region rules -> decide.

The main thing I was careful about is that the AI does NOT make the decision. It only reads the request and figures out the numbers. The actual approve/reject is done by normal code (the Rules class). I did it this way on purpose - a weight check on a real tower is a safety thing, so I want it to be exact and the same every time, not something an AI guessed. The AI is good at reading messy English, the code is good at the maths, so each does its part.

Because of that the whole thing also runs without any AI/API at all - it falls back to a regex parser. That's the mode the test cases use.

## Additional work that wasnt a part of the requirement

- Pounds vs kg. People write "33 lb" not always "15 kg". If you just read the number and treat it as kg you get the load wrong, so I convert lb to kg first.
- Same tower, multiple requests. Two requests can each fit on their own but not both. So once something's approved I "reserve" that weight, and the next request sees the real space left. Otherwise you'd approve two things that overload the tower.
- If I'm not confident I read the request right (missing info, weird text), I send it to a human even if it would've passed. Rather hold it than wrongly approve.
- Junk values like negative or zero weight get caught before the rules run.
- Every decision gets written to audit_log.jsonl so there's a record of what happened.

## The impact part

--batch also prints how many got auto-decided, how much yearly revenue the approved leases bring in, and roughly how much analyst time it saves vs someone checking each one by hand. The money/time numbers are assumptions I put at the top of the file - real numbers would just go there instead.

## Using the AI version (optional)

It works fine without this. But if you want to try the AI extraction, use an Anthropic key :

    pip install anthropic
    set ANTHROPIC_API_KEY=your-key        (Windows)
    export ANTHROPIC_API_KEY=your-key     (Mac/Linux)
    python tower_agent.py --llm "Du wants a 15kg antenna at 40 meters on TWR-101."

If there's no key it just uses the regex parser, so it never breaks.


## Files

|         File           |                    What it is                              |
|------------------------|------------------------------------------------------------|
| tower_agent.py         | the agent                                                  |
| towers_inventory.json  | the towers (120 of them, includes the 2 from the question) |
| regional_policies.txt  | the region rules                                           |
| gen_inventory.py       | what I used to make the tower list                         |
| audit_log.jsonl        | gets created when you run it, not part of the code         |