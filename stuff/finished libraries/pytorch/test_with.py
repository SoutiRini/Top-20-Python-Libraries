import os
import sys

from typing import Any

import torch
from torch.testing._internal.jit_utils import JitTestCase


# Make the helper files in test/ importable
pytorch_test_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(pytorch_test_dir)

if __name__ == "__main__":
    raise RuntimeError(
        "This test file is not meant to be run directly, use:\n\n"
        "\tpython test/test_jit.py TESTNAME\n\n"
        "instead."
    )


class TestWith(JitTestCase):
    """
    A suite of tests for with statements.
    """

    def test_with_as(self):
        """
        Check that with statements that use the 'as' keyword to bind expressions
        to targets work as expected.
        """
        global Context

        @torch.jit.script
        class Context(object):
            """
            This class implements a basic context manager interface for use in
            the unit tests. Unlike Context, the stateful part of this class
            is a Tensor that is mutated in-place so that modifications made in the
            JIT interpreter are visible outside of it.
            """

            def __init__(self, start: int):
                self.count = torch.tensor([start], dtype=torch.double)

            def __enter__(self):
                self.count.add_(0.3)
                return self.count

            def __exit__(self, type: Any, value: Any, tb: Any):
                self.count.sub_(0.3)

        def test_basic(x):
            # type: (Tensor) -> Tensor
            """Basic test with one with-statement."""

            c = Context(1)

            with c as mult:
                y = x + mult

            y *= c.count
            return y

        def test_pass(x):
            # type: (Tensor) -> Tensor
            """
            Test with a pass statement inside a with-statement. Although
            the body of the with is empty, __enter__ and __exit__ should
            still be called.
            """
            c = Context(1)

            with c as mult:
                pass

            x *= c.count
            return x

        def test_early_return(x, c):
            # type: (Tensor, Context) -> Tensor
            """
            Test that returning early from inside a with-statement works
            as expected.
            """
            with c as mult:
                y = x + mult
                return y

            x = y + y
            return x

        def test_conditional_early_return(x, c):
            # type: (Tensor, Context) -> Tensor
            """
            Test that conditionally returning early from inside a with-statement works
            as expected.
            """
            with c as mult:
                y = x + mult
                if mult > 0:
                    return y

            x = y + y
            return x

        def test_break(x, c, l):
            # type: (Tensor, Context, List[int]) -> Tensor
            """
            Test that breaking early from inside a with-statement works
            as expected.
            """
            with c as mult:
                for a in l:
                    if a == 0:
                        break
                    x += a * mult

            return x

        def test_continue(x, c, l):
            # type: (Tensor, Context, List[int]) -> Tensor
            """
            Test that using continue inside a with-statement works
            as expected.
            """
            with c as mult:
                for a in l:
                    if a == 0:
                        continue
                    x += a * mult

            return x

        def test_serial(x):
            # type: (Tensor) -> Tensor
            """
            Test two with-statements in a row.
            """
            c = Context(1)

            with c as mult:
                y = x + mult

            with c as mult:
                y *= mult

            return y

        def test_nested(x):
            # type: (Tensor) -> Tensor
            """
            Test nested with-statements.
            """
            c = Context(1)

            with c as m:
                with c as n:
                    y = x + n

                y *= m

            return y

        def test_combined(x):
            # type: (Tensor) -> Tensor
            """
            Test a with-statement with multiple with items.
            """
            c = Context(1)
            d = Context(2)

            with c as m, d as n:
                y = x + (m + n)

            return y

        test_input = torch.randn(2, 2)
        test_context = Context(2)
        test_list = [2, 0, 1, 3, 0, 2]

        self.checkScript(test_basic, (test_input,))
        self.checkScript(test_pass, (test_input,))
        self.checkScript(test_early_return, (test_input, test_context))
        self.checkScript(test_break, (test_input, test_context, test_list))
        self.checkScript(test_continue, (test_input, test_context, test_list))
        self.assertEqual(test_context.count, 2)
        self.checkScript(test_serial, (test_input,))
        self.checkScript(test_nested, (test_input,))
        self.checkScript(test_combined, (test_input,))

    def test_with_no_as(self):
        """
        Check that with statements that do not use the 'as' keyword to bind expressions
        to targets work as expected.
        """
        global Context

        @torch.jit.script
        class Context(object):
            """
            This class implements a basic context manager interface for use in
            the unit tests. Unlike Context, the stateful part of this class
            is a Tensor that is mutated in-place so that modifications made in the
            JIT interpreter are visible outside of it.
            """

            def __init__(self, start: int):
                self.count = torch.tensor([start], dtype=torch.double)

            def __enter__(self):
                self.count.add_(0.3)
                return self.count

            def __exit__(self, type: Any, value: Any, tb: Any):
                self.count.sub_(0.3)

        def test_basic(x):
            # type: (Tensor) -> Tensor
            """Basic test with one with-statement."""

            c = Context(1)

            with c:
                y = x + c.count

            y *= c.count
            return y

        def test_pass(x):
            # type: (Tensor) -> Tensor
            """
            Test with a pass statement inside a with-statement. Although
            the body of the with is empty, __enter__ and __exit__ should
            still be called.
            """
            c = Context(1)

            with c:
                pass

            x *= c.count
            return x

        def test_early_return(x, c):
            # type: (Tensor, Context) -> Tensor
            """
            Test that returning early from inside a with-statement works
            as expected.
            """
            with c:
                y = x + c.count
                return y

            x = y + y
            return x

        def test_conditional_early_return(x, c):
            # type: (Tensor, Context) -> Tensor
            """
            Test that conditionally returning early from inside a with-statement works
            as expected.
            """
            with c:
                y = x + c.count
                if c.count > 0:
                    return y

            x = y + y
            return x

        def test_break(x, c, l):
            # type: (Tensor, Context, List[int]) -> Tensor
            """
            Test that breaking early from inside a with-statement works
            as expected.
            """
            with c:
                for a in l:
                    if a == 0:
                        break
                    x += a * c.count

            return x

        def test_continue(x, c, l):
            # type: (Tensor, Context, List[int]) -> Tensor
            """
            Test that using continue inside a with-statement works
            as expected.
            """
            with c:
                for a in l:
                    if a == 0:
                        continue
                    x += a * c.count

            return x

        def test_serial(x):
            # type: (Tensor) -> Tensor
            """
            Test two with-statements in a row.
            """
            c = Context(1)

            with c:
                y = x + c.count

            with c:
                y *= c.count

            return y

        def test_nested(x):
            # type: (Tensor) -> Tensor
            """
            Test nested with-statements.
            """
            c = Context(1)

            with c:
                with c:
                    y = x + c.count

                y *= c.count

            return y

        def test_combined(x):
            # type: (Tensor) -> Tensor
            """
            Test a with-statement with multiple with items.
            """
            c = Context(1)
            d = Context(2)

            with c, d:
                y = x + (c.count + d.count)

            return y

        test_input = torch.randn(2, 2)
        test_context = Context(2)
        test_list = [2, 0, 1, 3, 0, 2]

        self.checkScript(test_basic, (test_input,))
        self.checkScript(test_pass, (test_input,))
        self.checkScript(test_early_return, (test_input, test_context))
        self.checkScript(test_break, (test_input, test_context, test_list))
        self.checkScript(test_continue, (test_input, test_context, test_list))
        self.assertEqual(test_context.count, 2)
        self.checkScript(test_serial, (test_input,))
        self.checkScript(test_nested, (test_input,))
        self.checkScript(test_combined, (test_input,))

    def test_with_exceptions(self):
        """
        Check that exceptions thrown in the bodies of with-statements are
        handled correctly.
        """

        @torch.jit.script
        class Context(object):
            """
            This class implements a basic context manager interface for use in
            the unit tests. Unlike Context, the stateful part of this class
            is a Tensor that is mutated in-place so that modifications made in the
            JIT interpreter are visible outside of it.
            """

            def __init__(self, start: int):
                self.count = torch.tensor([start], dtype=torch.double)

            def __enter__(self):
                self.count.add_(0.3)
                return self.count

            def __exit__(self, type: Any, value: Any, tb: Any):
                self.count.sub_(0.3)

        def method_that_raises():
            # type: () -> Tensor
            raise Exception()

        def test_exception(x, c):
            # type: (Tensor, Context) -> Tensor
            """
            Test the case in which an exception is thrown while executing the body of a with-statement.
            """
            with c as _:
                x += method_that_raises()

            return x

        def test_exception_nested(x, c):
            # type: (Tensor, Context) -> Tensor
            """
            Test the case in which an exception is thrown while executing the body of a nested with-statement.
            """
            with c as _:
                with c as _:
                    x += method_that_raises()

            return x

        def with_that_raises(c):
            # type: (Context) -> Tensor
            a = torch.tensor([1])

            with c as _:
                a += method_that_raises()

            return a

        def test_exception_fn_call(x, c):
            # type: (Tensor, Context) -> Tensor
            """
            Test the case in which an exception is thrown while there are active with-statements in two different
            frames.
            """
            with c as _:
                x += with_that_raises(c)

            return x

        c = Context(1)

        with self.assertRaises(Exception):
            test_exception(torch.randn(2), c)
        self.assertEqual(c.count, 1)

        with self.assertRaises(Exception):
            test_exception_nested(torch.randn(2), c)
        self.assertEqual(c.count, 1)

        with self.assertRaises(Exception):
            test_exception_fn_call(torch.randn(2), c)
        self.assertEqual(c.count, 1)

    def test_with_errors(self):
        """
        Check that errors related to with-statements are detected and reported correctly.
        """

        @torch.jit.script
        class NoEnterNoExit(object):
            """
            This class is missing __enter__ and __exit__ methods.
            """

            def __init__(self):
                self.count = 1

        @torch.jit.script
        class BadEnter(object):
            """
            This class is has an __enter__ method with an incorrect signature.
            """

            def __init__(self):
                self.count = 1

            def __enter__(self, incr: int):
                self.count += incr

            def __exit__(self, type: Any, value: Any, tb: Any):
                pass

        @torch.jit.script
        class BadExit(object):
            """
            This class is has an __exit__ method with an incorrect signature.
            """

            def __init__(self):
                self.count = 1

            def __enter__(self):
                self.count += 1

            def __exit__(self, type: Any, value: Any):
                pass

        @torch.jit.script
        class ExitIncorrectTypes(object):
            """
            This class is has an __exit__ method with unsupported argument types.
            """

            def __init__(self):
                self.count = 1

            def __enter__(self):
                self.count += 1

            def __exit__(self, type: Any, value: int, tb: int):
                pass

        def test_no_enter_no_exit(x, c):
            # type: (Tensor, NoEnterNoExit) -> Tensor
            with c as _:
                pass

            return x

        def test_bad_enter(x, c):
            # type: (Tensor, BadEnter) -> Tensor
            with c as _:
                pass

            return x

        def test_bad_exit(x, c):
            # type: (Tensor, BadExit) -> Tensor
            with c as _:
                pass

            return x

        def test_exit_incorrect_types(x, c):
            # type: (Tensor, ExitIncorrectTypes) -> Tensor
            with c as _:
                pass

            return x

        test_tensor = torch.randn(5, dtype=torch.double)

        with self.assertRaisesRegex(
            RuntimeError, r"does not define __enter__ and __exit__ methods"
        ):
            self.checkScript(test_no_enter_no_exit, (test_tensor, NoEnterNoExit()))

        with self.assertRaisesRegex(
            RuntimeError, r"__enter__ must have only one argument and one return value"
        ):
            self.checkScript(test_bad_enter, (test_tensor, BadEnter()))

        with self.assertRaisesRegex(
            RuntimeError, r"__exit__ must have four arguments and no return value"
        ):
            self.checkScript(test_bad_exit, (test_tensor, BadExit()))

        with self.assertRaisesRegex(
            RuntimeError, r"argument 2 of __exit__ must have Any type"
        ):
            self.checkScript(
                test_exit_incorrect_types, (test_tensor, ExitIncorrectTypes())
            )
