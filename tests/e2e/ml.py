"""A multi-line prompt like an agent's: Enter adds a line, Esc Enter submits.

Prints the exact text it received, so the tests can compare it.
"""
from prompt_toolkit import prompt

while True:
    text = prompt('> ', multiline=True, prompt_continuation='. ')
    print('RESULT=' + repr(text))
