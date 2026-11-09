"""
kbot.learning — self-learning loop.

Design principle: "self-learning" here means a closed feedback loop that
measures whether the model's probabilities match reality, corrects them,
and only adopts changes that prove out on out-of-sample data.  It does NOT
mean a black box that prints more money the longer it runs.  The bot must
NEVER increase position sizing because of a winning streak, and must NEVER
auto-promote a new model/params without passing a validation gate.  If no
edge exists, learning cannot invent one.
"""
