# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Line-profile subpackage.

Customer picks slow functions from the first-pass results and reruns the same
flow with ``line_profiler`` attached only to the chosen functions. Results
merge into the parent Optimus Session as an `Optimus Phase Two Run` child row.
"""
