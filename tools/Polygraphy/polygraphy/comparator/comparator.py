#
# SPDX-FileCopyrightText: Copyright (c) 1993-2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import contextlib
import copy
import queue
from multiprocessing import Process, Queue

from polygraphy import mod, util
from polygraphy.common import TensorMetadata
from polygraphy.comparator import util as comp_util
from polygraphy.comparator.compare import CompareFunc
from polygraphy.comparator.data_loader import DataLoader, DataLoaderCache
from polygraphy.comparator.struct import AccuracyResult, IterationResult, RunResults
from polygraphy.logger import G_LOGGER, LogMode

np = mod.lazy_import("numpy")


@mod.export()
class Comparator:
    """
    Compares inference outputs.
    """

    @staticmethod
    def run(
        runners,
        data_loader=None,
        warm_up=None,
        use_subprocess=None,
        subprocess_timeout=None,
        subprocess_polling_interval=None,
        save_inputs_path=None,
    ):
        """
        Runs the supplied runners sequentially.

        Args:
            runners (List[BaseRunner]):
                    A list of runners to run.
            data_loader (Sequence[OrderedDict[str, numpy.ndarray]]):
                    A generator or iterable that yields a dictionary that maps input names to input numpy buffers.
                    In the simplest case, this can be a `List[Dict[str, numpy.ndarray]]` .

                    In case you don't know details about the inputs ahead of time, you can access the
                    `input_metadata` property in your data loader, which will be set to an `TensorMetadata`
                    instance by this function.
                    Note that this does not work for generators or lists.

                    The number of iterations run by this function is controlled by the number of items supplied
                    by the data loader.

                    Defaults to an instance of `DataLoader`.
            warm_up (int):
                    The number of warm up runs to perform for each runner before timing.
                    Defaults to 0.
            use_subprocess (bool):
                    Whether each runner should be run in a subprocess. This allows each runner to have exclusive
                    access to the GPU. When using a subprocess, runners and loaders will never be modified.
            subprocess_timeout (int):
                    The timeout before a subprocess is killed automatically. This is useful for handling processes
                    that never terminate. A value of None disables the timeout. Defaults to None.
            subprocess_polling_interval (int):
                    The polling interval, in seconds, for checking whether a subprocess has completed or crashed.
                    In rare cases, omitting this parameter when subprocesses are enabled may cause this function
                    to hang indefinitely if the subprocess crashes.
                    A value of 0 disables polling. Defaults to 30 seconds.
            save_inputs_path (str):
                    Path at which to save inputs used during inference. This will include all inputs generated by
                    the provided data_loader, and will be saved as a JSON List[Dict[str, numpy.ndarray]].

        Returns:
            RunResults:
                    A mapping of runner names to the results of their inference.
                    The ordering of `runners` is preserved in this mapping.
        """
        warm_up = util.default(warm_up, 0)
        data_loader = util.default(data_loader, DataLoader())
        use_subprocess = util.default(use_subprocess, False)
        subprocess_polling_interval = util.default(subprocess_polling_interval, 30)
        loader_cache = DataLoaderCache(data_loader, save_inputs_path=save_inputs_path)

        def execute_runner(runner, loader_cache):
            with runner as active_runner:
                # DataLoaderCache will ensure that the feed_dict does not contain any extra entries
                # based on the provided input_metadata.
                loader_cache.set_input_metadata(active_runner.get_input_metadata())

                if warm_up:
                    G_LOGGER.start(f"{active_runner.name:35} | Running {warm_up} warm-up run(s)")
                    try:
                        feed_dict = loader_cache[0]
                    except IndexError:
                        G_LOGGER.warning(
                            f"{warm_up} warm-up run(s) were requested, but data loader did not supply any data. Skipping warm-up run(s)"
                        )
                    else:
                        G_LOGGER.ultra_verbose(f"Warm-up Input Buffers:\n{util.indent_block(feed_dict)}")
                        # First do a few warm-up runs, and don't time them.
                        for _ in range(warm_up):
                            active_runner.infer(feed_dict=feed_dict)
                    G_LOGGER.finish(f"{active_runner.name:35} | Finished {warm_up} warm-up run(s)")

                # Then, actual iterations.
                index = 0
                iteration_results = []

                total_runtime = 0
                for index, feed_dict in enumerate(loader_cache):
                    G_LOGGER.info(
                        f"{active_runner.name:35}\n---- Inference Input(s) ----\n{TensorMetadata().from_feed_dict(feed_dict)}",
                        mode=LogMode.ONCE,
                    )

                    G_LOGGER.extra_verbose(
                        lambda: f"{active_runner.name:35} | Feeding inputs:\n{util.indent_block(dict(feed_dict))}"
                    )
                    outputs = active_runner.infer(feed_dict=feed_dict)

                    runtime = active_runner.last_inference_time()
                    total_runtime += runtime
                    # Without a deep copy here, outputs will always reference the output of the last run
                    iteration_results.append(
                        IterationResult(outputs=copy.deepcopy(outputs), runtime=runtime, runner_name=active_runner.name)
                    )

                    G_LOGGER.info(
                        f"{active_runner.name:35}\n---- Inference Output(s) ----\n{TensorMetadata().from_feed_dict(outputs)}",
                        mode=LogMode.ONCE,
                    )
                    G_LOGGER.extra_verbose(
                        lambda: f"{active_runner.name:35} | Inference Time: {runtime * 1000.0:.3f} ms | Received outputs:\n{util.indent_block(dict(outputs))}"
                    )

                total_runtime_ms = total_runtime * 1000.0
                G_LOGGER.finish(
                    f"{active_runner.name:35} | Completed {index + 1} iteration(s) in {total_runtime_ms:.4g} ms | Average inference time: {total_runtime_ms / float(index + 1):.4g} ms."
                )
                return iteration_results

        # Wraps execute_runner to use a queue.
        def execute_runner_with_queue(runner_queue, runner, loader_cache):
            iteration_results = None
            try:
                iteration_results = execute_runner(runner, loader_cache)
            except:
                # Cannot necessarily send the exception back over the queue.
                G_LOGGER.backrace()
            util.try_send_on_queue(runner_queue, iteration_results)
            # After finishing, send the updated loader_cache back.
            util.try_send_on_queue(runner_queue, loader_cache)

        # Do all inferences in one loop, then comparisons at a later stage.
        # We run each runner in a separate process so that we can provide exclusive GPU access for each runner.
        run_results = RunResults()

        if not runners:
            G_LOGGER.warning(
                "No runners were provided to Comparator.run(). Inference will not be run, and run results will be empty."
            )

        for runner in runners:
            G_LOGGER.start(f"{runner.name:35} | Activating and starting inference")
            if use_subprocess:
                runner_queue = Queue()
                process = Process(target=execute_runner_with_queue, args=(runner_queue, runner, loader_cache))
                process.start()

                # If a subprocess hangs in a certain way, then process.join could block forever. Hence,
                # we need to keep polling the process to make sure it really is alive.
                iteration_results = None
                while process.is_alive() and iteration_results is None:
                    try:
                        iteration_results = util.try_receive_on_queue(
                            runner_queue, timeout=subprocess_polling_interval / 2
                        )
                        # Receive updated loader cache, or fall back if it could not be sent.
                        loader_cache = util.try_receive_on_queue(runner_queue, timeout=subprocess_polling_interval / 2)
                    except queue.Empty:
                        G_LOGGER.extra_verbose("Polled subprocess - still running")

                try:
                    assert iteration_results is not None
                    run_results.append((runner.name, iteration_results))
                    process.join(subprocess_timeout)
                except:
                    G_LOGGER.critical(
                        f"{runner.name:35} | Terminated prematurely. Check the exception logged above. If there is no exception logged above, make sure not to use the --use-subprocess flag or set use_subprocess=False in Comparator.run()."
                    )
                finally:
                    process.terminate()

                if loader_cache is None:
                    G_LOGGER.critical(
                        "Could not send data loader cache to runner subprocess. Please try disabling subprocesses "
                        "by removing the --use-subprocess flag, or setting use_subprocess=False in Comparator.run()"
                    )
            else:
                run_results.append((runner.name, execute_runner(runner, loader_cache)))

        G_LOGGER.verbose(f"Successfully ran: {[r.name for r in runners]}")
        return run_results

    @staticmethod
    def postprocess(run_results, postprocess_func):
        """
        Applies post processing to all the outputs in the provided run results.
        This is a convenience function to avoid the need for manual iteration over the run_results dictionary.

        Args:
            run_results (RunResults): The result of Comparator.run().
            postprocess_func (Callable(IterationResult) -> IterationResult):
                    The function to apply to each ``IterationResult``.

        Returns:
            RunResults: The updated run results.
        """
        G_LOGGER.start(f"Applying post-processing to outputs: {postprocess_func.__name__}")
        for _, iteration_results in run_results:
            for index, iter_res in enumerate(iteration_results):
                iteration_results[index] = postprocess_func(iter_res)
        G_LOGGER.finish("Finished applying post-processing")
        return run_results

    @staticmethod
    def default_comparisons(run_results):
        # Sets up default comparisons - which is to compare each runner to the subsequent one.
        return [(i, i + 1) for i in range(len(run_results) - 1)]

    @staticmethod
    def compare_accuracy(run_results, fail_fast=False, comparisons=None, compare_func=None):
        """
        Args:
            run_results (RunResults): The result of Comparator.run()


            fail_fast (bool): Whether to exit after the first failure
            comparisons (List[Tuple[int, int]]):
                    Comparisons to perform, specified by runner indexes. For example, [(0, 1), (1, 2)]
                    would compare the first runner with the second, and the second with the third.
                    By default, this compares each result to the subsequent one.
            compare_func (Callable(IterationResult, IterationResult) -> OrderedDict[str, bool]):
                    A function that takes in two IterationResults, and returns a dictionary that maps output
                    names to a boolean (or anything convertible to a boolean) indicating whether outputs matched.
                    The order of arguments to this function is guaranteed to be the same as the ordering of the
                    tuples contained in `comparisons`.

        Returns:
            AccuracyResult:
                    A summary of the results of the comparisons. The order of the keys (i.e. runner pairs) is
                    guaranteed to be the same as the order of `comparisons`. For more details, see the AccuracyResult
                    docstring (e.g. help(AccuracyResult)).
        """

        def find_mismatched(match_dict):
            return [name for name, matched in match_dict.items() if not bool(matched)]

        compare_func = util.default(compare_func, CompareFunc.simple())
        comparisons = util.default(comparisons, Comparator.default_comparisons(run_results))

        accuracy_result = AccuracyResult()
        for runner0_index, runner1_index in comparisons:
            (runner0_name, results0), (runner1_name, results1) = run_results[runner0_index], run_results[runner1_index]

            G_LOGGER.start(f"Accuracy Comparison | {runner0_name} vs. {runner1_name}")
            with G_LOGGER.indent():
                runner_pair = (runner0_name, runner1_name)
                accuracy_result[runner_pair] = []

                num_iters = min(len(results0), len(results1))
                for iteration, (result0, result1) in enumerate(zip(results0, results1)):
                    if num_iters > 1:
                        G_LOGGER.info(f"Iteration: {iteration}")
                    with contextlib.ExitStack() as stack:
                        if num_iters > 1:
                            stack.enter_context(G_LOGGER.indent())
                        iteration_match_dict = compare_func(result0, result1)
                        accuracy_result[runner_pair].append(iteration_match_dict)

                        mismatched_outputs = find_mismatched(iteration_match_dict)
                        if fail_fast and mismatched_outputs:
                            return accuracy_result

                G_LOGGER.extra_verbose(f"Finished comparing {runner0_name} with {runner1_name}")

            passed, _, total = accuracy_result.stats(runner_pair)
            pass_rate = accuracy_result.percentage(runner_pair) * 100.0

            msg = f"Accuracy Summary | {runner0_name} vs. {runner1_name} | Passed: {passed}/{total} iterations | Pass Rate: {pass_rate}%"
            if passed == total:
                G_LOGGER.finish(msg)
            else:
                G_LOGGER.error(msg)

        return accuracy_result

    @staticmethod
    def validate(run_results, check_inf=None, check_nan=None, fail_fast=None):
        """
        Checks output validity.

        Args:
            run_results (Dict[str, List[IterationResult]]): The result of Comparator.run().
            check_inf (bool): Whether to fail on Infs. Defaults to False.
            check_nan (bool): Whether to fail on NaNs. Defaults to True.
            fail_fast (bool): Whether to fail after the first invalid value. Defaults to False.

        Returns:
            bool: True if all outputs were valid, False otherwise.
        """
        check_inf = util.default(check_inf, False)
        check_nan = util.default(check_nan, True)
        fail_fast = util.default(fail_fast, False)

        def is_finite(output):
            non_finite = np.logical_not(np.isfinite(output))
            if np.any(non_finite):
                G_LOGGER.error("Inf Detected | One or more non-finite values were encountered in this output")
                G_LOGGER.info(
                    "Note: Use -vv or set logging verbosity to EXTRA_VERBOSE to display non-finite values",
                    mode=LogMode.ONCE,
                )
                G_LOGGER.extra_verbose(f"Note: non-finite values at:\n{non_finite}")
                G_LOGGER.extra_verbose(f"Note: non-finite values:\n{output[non_finite]}")
                return False
            return True

        def is_not_nan(output):
            nans = np.isnan(output)
            if np.any(nans):
                G_LOGGER.error("NaN Detected | One or more NaNs were encountered in this output")
                G_LOGGER.info(
                    "Note: Use -vv or set logging verbosity to EXTRA_VERBOSE to display locations of NaNs",
                    mode=LogMode.ONCE,
                )
                G_LOGGER.extra_verbose(f"Note: NaNs at:\n{nans}")
                return False
            return True

        def validate_output(runner_name, output_name, output):
            G_LOGGER.start(
                f"{runner_name:35} | Validating output: {output_name} (check_inf={check_inf}, check_nan={check_nan})"
            )
            with G_LOGGER.indent():
                comp_util.log_output_stats(output)

                output_valid = True
                if check_nan:
                    output_valid &= is_not_nan(output)
                if check_inf:
                    output_valid &= is_finite(output)

                if output_valid:
                    G_LOGGER.finish(f"PASSED | Output: {output_name} is valid")
                else:
                    G_LOGGER.error(f"FAILED | Errors detected in output: {output_name}")
                return output_valid

        all_valid = True
        G_LOGGER.start(f"Output Validation | Runners: {list(run_results.keys())}")
        with G_LOGGER.indent():
            for runner_name, results in run_results:
                for result in results:
                    for output_name, output in result.items():
                        all_valid &= validate_output(runner_name, output_name, output)
                        if fail_fast and not all_valid:
                            return False

            if all_valid:
                G_LOGGER.finish("PASSED | Output Validation")
            else:
                G_LOGGER.error("FAILED | Output Validation")

        return all_valid
