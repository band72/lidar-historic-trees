# MIT License
#
# Copyright (c) 2024 Meher Malladi, Luca Lobefaro, Tiziano Guadagnino, Cyrill Stachniss
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Exception hierarchy for the forest inventory pipeline.

Every error raised deliberately by the pipeline derives from :class:`FIPError`,
so callers (CLI, web app) can catch one type and show a clean message, while
unexpected exceptions still surface with a full traceback.
"""


class FIPError(Exception):
    """Base class for all pipeline errors."""


class InputDataError(FIPError):
    """The input point cloud is missing, unreadable, empty or malformed."""


class ParameterError(FIPError, ValueError):
    """A user-supplied parameter is outside its valid range."""


class StageError(FIPError):
    """A pipeline stage failed. ``stage`` names which one."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"[{stage}] {message}")


class FitError(FIPError):
    """A single tree could not be fitted. Caught per tree; never aborts a run."""


class ExportError(FIPError):
    """Writing an output file (CSV, DXF, ...) failed."""
