#!/usr/bin/env python3

import argparse
import numpy as np
import json
import pandas as pd
import scipy.io
import scipy.signal
import sys
import videojitter.util


def parse_arguments():
    argument_parser = argparse.ArgumentParser(
        description="Given a spec file and recorded light waveform file, analyzes the recording and outputs the results to stdout in CSV format.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    argument_parser.add_argument(
        "--spec-file",
        help="Path to the input spec file",
        required=True,
        type=argparse.FileType(),
        default=argparse.SUPPRESS,
    )
    argument_parser.add_argument(
        "--recording-file",
        help="Path to the input recording file",
        required=True,
        type=argparse.FileType(mode="rb"),
        default=argparse.SUPPRESS,
    )
    argument_parser.add_argument(
        "--min-edge-separation-seconds",
        help="The minimum time interval between edges that the analyzer will be able to resolve. Note that this is the theoretical limit; in practice, this needs to include a safety margin of about 2x to account for the non-ideal response of the downsampling and upsampling filters. Higher values improve high frequency noise rejection but make it more likely the analyzer will fail to detect edges, and may slightly degrade timestamp resolution.",
        type=float,
        default=0.0005,
    )
    argument_parser.add_argument(
        "--boundaries-signal-frames",
        help="The length of the reference signal used to detect the beginning and end of the test signal within the recording, in nominal frame durations.",
        type=int,
        default=11,
    )
    argument_parser.add_argument(
        "--boundaries-score-threshold-ratio",
        help="How well does a given portion of the recording have to match the reference sequence in order for it to be considered as the beginning or end of the test signal, as a ratio of the best match anywhere in the recording.",
        type=float,
        default=0.5,
    )
    argument_parser.add_argument(
        "--timestamp-resolution-seconds",
        help="The desired resolution of the resulting transition timestamps, in seconds. This determines the upsampling ratio used before looking for edges. Higher values will reduce transition timing resolution but will make processing faster and less memory intensive.",
        type=float,
        default=0.00001,
    )
    argument_parser.add_argument(
        "--minimum-negative-slope-peak-height-ratio",
        help="The minimum height required for a negative slope peak to be recorded as a falling edge, relative to the overall maximum negative slope.",
        type=float,
        default=0.3,
    )
    argument_parser.add_argument(
        "--minimum-positive-slope-peak-height-ratio",
        help="The minimum height required for a positive slope peak to be recorded as a rising edge, relative to the overall maximum negative slope.",
        type=float,
        default=0.3,
    )
    argument_parser.add_argument(
        "--minimum-negative-slope-peak-prominence-ratio",
        help="The minimum prominence required for a negative slope peak to be recorded as a falling edge, relative to the overall maximum negative slope.",
        type=float,
        default=0.3,
    )
    argument_parser.add_argument(
        "--minimum-positive-slope-peak-prominence-ratio",
        help="The minimum prominence required for a positive slope peak to be recorded as a rising edge, relative to the overall maximum negative slope.",
        type=float,
        default=0.3,
    )
    argument_parser.add_argument(
        "--output-downsampled-slope-file",
        help="(Only useful for debugging) Write the first derivative (slope) of the downsampled recording as a WAV file to the given path",
        type=argparse.FileType(mode="wb"),
    )
    argument_parser.add_argument(
        "--output-boundaries-signal-file",
        help="(Only useful for debugging) Write the boundaries reference signal as a WAV file to the given path",
        type=argparse.FileType(mode="wb"),
    )
    argument_parser.add_argument(
        "--output-cross-correlation-file",
        help="(Only useful for debugging) Write the cross-correlation of recording against the boundaries reference signal as a WAV file to the given path",
        type=argparse.FileType(mode="wb"),
    )
    argument_parser.add_argument(
        "--output-boundary-candidates-file",
        help="(Only useful for debugging) Write the boundary candidates as a WAV file to the given path",
        type=argparse.FileType(mode="wb"),
    )
    argument_parser.add_argument(
        "--output-trimmed-slope-file",
        help="(Only useful for debugging) Write the trimmed recording as a WAV file to the given path",
        type=argparse.FileType(mode="wb"),
    )
    argument_parser.add_argument(
        "--output-upsampled-slope-file",
        help="(Only useful for debugging) Write the recording slope as a WAV file to the given path",
        type=argparse.FileType(mode="wb"),
    )
    argument_parser.add_argument(
        "--output-edges-file",
        help="(Only useful for debugging) Write the estimated edges as a WAV file to the given path",
        type=argparse.FileType(mode="wb"),
    )
    return argument_parser.parse_args()


def generate_boundaries_reference_samples(frame_count, fps_num, fps_den, sample_rate):
    return videojitter.util.generate_fake_samples(
        np.tile([False, True], int(np.ceil(frame_count / 2)))[0:frame_count],
        fps_num,
        fps_den,
        sample_rate,
    )


def find_edges(
    slope,
    minimum_negative_height,
    minimum_positive_height,
    minimum_falling_edge_distance,
    minimum_rising_edge_distance,
    minimum_negative_prominence,
    minimum_positive_prominence,
):
    """Looks for edges in the recording slope signal.

    Returns a Series whose index is the offset into `slope` and whose values
    are booleans indicating a falling edge (False) or a rising edge (True).
    """
    falling_edge_indexes = scipy.signal.find_peaks(
        -slope,
        height=minimum_negative_height,
        distance=minimum_falling_edge_distance,
        prominence=minimum_negative_prominence,
    )[0]
    rising_edge_indexes = scipy.signal.find_peaks(
        slope,
        height=minimum_positive_height,
        distance=minimum_rising_edge_distance,
        prominence=minimum_positive_prominence,
    )[0]
    return pd.Series(
        np.concatenate(
            [
                np.repeat(False, falling_edge_indexes.size),
                np.repeat(True, rising_edge_indexes.size),
            ]
        ),
        index=pd.Index(
            np.concatenate([falling_edge_indexes, rising_edge_indexes]), name="offset"
        ),
        name="frame",
    ).sort_index()


def generate_downsampling_derivative_kernel(downsampling_ratio):
    return scipy.signal.convolve(
        scipy.signal.firwin(
            numtaps=2 * int(2 * 10 * downsampling_ratio) + 1,
            cutoff=1 / downsampling_ratio,
        ),
        # Take first derivative (differentiation). See
        # https://terpconnect.umd.edu/~toh/spectrum/Convolution.html
        [1, -1],
    )


def analyze_recording():
    args = parse_arguments()
    assert (
        args.boundaries_signal_frames % 2 != 0
    ), "The number of frames in the boundaries reference signal should be odd so that the signal begins and ends on the same frame"
    spec = json.load(args.spec_file)
    nominal_fps = spec["fps"]["num"] / spec["fps"]["den"]
    frames = videojitter.util.generate_frames(
        spec["transition_count"], spec["delayed_transitions"]
    )
    reference_duration_seconds = len(frames) / nominal_fps
    print(
        f"Successfully loaded spec file describing {len(frames)} frames at {nominal_fps} FPS ({reference_duration_seconds} seconds)",
        file=sys.stderr,
    )

    recording_sample_rate, recording_samples = scipy.io.wavfile.read(
        args.recording_file
    )
    recording_duration_seconds = recording_samples.size / recording_sample_rate
    print(
        f"Successfully loaded recording containing {recording_samples.size} samples at {recording_sample_rate} Hz ({recording_duration_seconds} seconds)",
        file=sys.stderr,
    )

    def format_index(index):
        return f"sample {index} ({index / recording_sample_rate} seconds)"

    def maybe_write_wavfile(file, samples, normalize=False):
        if not file:
            return
        if normalize:
            samples = samples / np.max(np.abs(samples))
        scipy.io.wavfile.write(
            file, int(recording_sample_rate), samples.astype(np.float32)
        )

    # A rising edge is always followed by a falling edge, so the period is twice
    # the distance between edges. Therefore, in order to resolve a given edge
    # interval, we need to be able to resolve a signal of a frequency that is
    # half the edge frequency. The minimum sample rate is double that (Nyquist),
    # so these cancel each other out.
    downsampling_ratio = np.floor(
        recording_sample_rate / (1 / args.min_edge_separation_seconds)
    )
    recording_sample_rate /= downsampling_ratio
    print(
        f"Downsampling recording by {downsampling_ratio}x (to {recording_sample_rate} Hz)",
        file=sys.stderr,
    )
    recording_slope = scipy.signal.resample_poly(
        recording_samples,
        up=1,
        down=downsampling_ratio,
        window=generate_downsampling_derivative_kernel(downsampling_ratio),
    )
    maybe_write_wavfile(args.output_downsampled_slope_file, recording_slope)

    boundaries_reference_samples = generate_boundaries_reference_samples(
        args.boundaries_signal_frames,
        spec["fps"]["num"],
        spec["fps"]["den"],
        recording_sample_rate,
    )
    maybe_write_wavfile(
        args.output_boundaries_signal_file, boundaries_reference_samples
    )

    cross_correlation = scipy.signal.correlate(
        recording_slope, boundaries_reference_samples, mode="valid"
    )
    maybe_write_wavfile(
        args.output_cross_correlation_file,
        cross_correlation,
        normalize=True,
    )

    abs_cross_correlation = np.abs(cross_correlation)
    boundary_candidates = (
        abs_cross_correlation
        >= np.max(abs_cross_correlation) * args.boundaries_score_threshold_ratio
    )
    maybe_write_wavfile(
        args.output_boundary_candidates_file,
        boundary_candidates,
    )

    boundary_candidate_indexes = np.nonzero(boundary_candidates)[0]
    assert boundary_candidate_indexes.size > 1
    test_signal_start_index = boundary_candidate_indexes[0]
    test_signal_end_index = (
        boundary_candidate_indexes[-1] + boundaries_reference_samples.size
    )
    print(
        f"Test signal appears to start at {format_index(test_signal_start_index)} and end at {format_index(test_signal_end_index)} in the recording.",
        file=sys.stderr,
    )

    recording_slope = recording_slope[test_signal_start_index:test_signal_end_index]
    maybe_write_wavfile(args.output_trimmed_slope_file, recording_slope)

    upsampling_ratio = np.ceil(
        (1 / args.timestamp_resolution_seconds) / recording_sample_rate
    )
    recording_sample_rate *= upsampling_ratio
    test_signal_start_index *= upsampling_ratio
    test_signal_end_index *= upsampling_ratio
    print(
        f"Upsampling recording slope by {upsampling_ratio}x to {recording_sample_rate} Hz",
        file=sys.stderr,
    )
    recording_slope = scipy.signal.resample_poly(
        recording_slope,
        up=upsampling_ratio,
        down=1,
    )
    maybe_write_wavfile(args.output_upsampled_slope_file, recording_slope)

    recording_slope_min = recording_slope.min()
    recording_slope_max = recording_slope.max()
    print(
        f"Recording slope range: [{recording_slope_min}, {recording_slope_max}]",
        file=sys.stderr,
    )

    minimum_edge_distance_samples = int(
        args.min_edge_separation_seconds * 2 * recording_sample_rate
    )
    find_edge_params = {
        "minimum_negative_height": -recording_slope_min
        * args.minimum_negative_slope_peak_height_ratio,
        "minimum_positive_height": recording_slope_max
        * args.minimum_positive_slope_peak_height_ratio,
        "minimum_falling_edge_distance": minimum_edge_distance_samples,
        "minimum_rising_edge_distance": minimum_edge_distance_samples,
        "minimum_negative_prominence": -recording_slope_min
        * args.minimum_negative_slope_peak_prominence_ratio,
        "minimum_positive_prominence": recording_slope_max
        * args.minimum_positive_slope_peak_prominence_ratio,
    }
    print(
        f"Finding edges with parameters: {find_edge_params}",
        file=sys.stderr,
    )
    edges = find_edges(recording_slope, **find_edge_params)
    print(f"Detected {edges.index.size} edges (frame transitions).", file=sys.stderr)
    assert edges.index.size > 0

    if args.output_edges_file:
        is_edge = np.zeros(recording_slope.size)
        is_edge[edges.index] = edges * 2 - 1
        maybe_write_wavfile(args.output_edges_file, is_edge)

    first_edge = edges.iloc[0]
    last_edge = edges.iloc[-1]
    if first_edge == last_edge:
        print(
            f"WARNING: the first and last edges are both {'rising' if first_edge else 'falling'}. This doesn't make sense as the first and last frames of the test video are supposed to be both black. Unable to determine transition directions as a result.",
            file=sys.stderr,
        )
    else:
        print(
            f"First edge is {'rising' if first_edge else 'falling'} and last edge is {'rising' if last_edge else 'falling'}. Deducing that a falling edge means a transition to {'black' if first_edge else 'white'} and a rising edge means a transition to {'white' if first_edge else 'black'}.",
            file=sys.stderr,
        )
        if not first_edge:
            edges = ~edges

    edges.index = (edges.index + test_signal_start_index) / recording_sample_rate
    edges.rename_axis("recording_timestamp_seconds").to_csv(sys.stdout)


analyze_recording()
