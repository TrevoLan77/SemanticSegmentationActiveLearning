"""
This script creates a set of tensorflow records for each image- label- pair.

python generate_data.py --data_set {kitti,cityscapes,freiburg} \
                        --root_dir /path/to/dataset/root
"""
import os
import sys
import multiprocessing

import argparse

import numpy as np
import tensorflow as tf


config = tf.ConfigProto()
config.gpu_options.visible_device_list = ""
tf.enable_eager_execution(config=config)
show_progress = False
try:
    from tqdm import tqdm
    show_progress = True
except ImportError:
    pass
try:
    import cv2
except ImportError:
    pass

def _bytes_feature(value):
    """Returns a bytes_list from a string / byte."""
    _bytes = None
    if isinstance(value, str):
        _bytes = value.encode()
    else:
        _bytes = value
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[_bytes]))

def _int64_feature(value):
    """Returns an int64_list from a bool / enum / int / uint."""
    return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))

support = None; split_path = None
def proc_initializer(args, path):
    global support, split_path
    split_path = path
    if args.dataset[0].lower() == "cityscapes":
        from support.cityscapes import SupportLayer
        use_coarse = True if args.extra is not None and \
                             "coarse" in args.extra[0].lower() \
                     else False
        support = SupportLayer(use_coarse)
    elif args.dataset[0].lower() == "freiburg":
        from support.freiburg import SupportLayer
        modalities = None if args.extra is None else args.extra
        support = SupportLayer(modalities)
    else:
        raise ValueError("Invalid argument \"dataset\": %s" % args.dataset[0])


def process_image(path):
    ext = path.split(".")[-1]
    # return image, shape, ext
    encoding  = tf.io.read_file(path)
    # Seperate heads for decoding png or jpg
    if ext == "png":
        decoding = tf.image.decode_png(encoding)
    elif ext == "jpg" or ext == "jpeg":
        ext = "jpg"
        decoding = tf.image.decode_jpeg(encoding)
    elif ext == "tif" or ext == "tiff":
        ext = "tif"
        decoding = cv2.imread(path, -1)
        if len(decoding.shape) == 3 and decoding.shape[2] == 3:
            decoding = cv2.cvtColor(cv2.COLOR_BGR2RGB)
        elif len(decoding.shape) == 2:
            decoding = np.expand_dims(decoding, axis=-1)
        encoding = tf.image.encode_png(decoding)
    else:
        raise ValueError(
            "Unsupported image format \"%s\"" % ext)
    shape = np.array(decoding.shape)
    return encoding, shape, ext

def record_example(example):
    # example = [str(ID), dict({str(type): str(path)})]
    features = {}
    shapes   = []
    for _type in example[1].keys():
        # Only "label" key need to be treated differently all other
        # is assumed to contain image data (rgb/nir/depthmap)
        path = example[1][_type]
        ext  = path.split(".")[-1] # path extension
        if "label" in _type: # label data
            # Check file extension
            if ext != "png":
                raise ValueError(
                    "The label images need to be png files!"
                    "Got \"%s\"" % ext)
            label_encoding = tf.io.read_file(path)
            label_decoding = tf.image.decode_png(label_encoding)
            # Remapping of labels (can only be png)
            label_decoding = np.array(label_decoding)
            LUT = support.get_label_mapping()
            if np.array(label_decoding.shape)[-1] == 3:
                # use green channel
                label_decoding = label_decoding[:,:,1].astype(np.int32)
                label_decoding = np.expand_dims(label_decoding, axis=-1)
            # A bit awkward, but TF won't let do tf.nn.embedding_lookup
            label_decoding = LUT[label_decoding]

            label_encoding  = tf.image.encode_png(label_decoding)
            shape = np.array(label_decoding.shape)
            features["label"] = _bytes_feature(label_encoding.numpy())
        else: # image data
            image, shape, ext = process_image(path)
            if len(shape) == 3:
                channels = shape[2]
            else:
                channels = 1
            # note that @_type/data is the raw image encoding
            features[_type + "/channels"] = _int64_feature(channels)
            features[_type + "/data"]     = _bytes_feature(image.numpy())
            features[_type + "/encoding"] = _bytes_feature(ext)
        shapes.append(shape)
    # END for _type in example[1].keys()
    # Check that shapes are consistent
    for i in range(1,len(shapes)):
        if shapes[i][0] != shapes[i-1][0] or \
           shapes[i][1] != shapes[i-1][1]:
            raise ValueError(
                "Image dimensions does not match label.\n"
                "Got: %s" % shapes)
    # Add shape info to feature. Note that channels are allready added
    # and label image is assumed to be single channel png image.
    features["height"] = _int64_feature(shape[0])
    features["width"]  = _int64_feature(shape[1])
    # Construct feature example
    tf_features = tf.train.Features(feature=features)
    tf_example  = tf.train.Example(features=tf_features)
    filename = example[0] + ".tfrecord"
    with tf.io.TFRecordWriter(
            os.path.join(split_path, filename)) as f:
        f.write(tf_example.SerializeToString())
    return features


def main(args):
    if args.dataset[0].lower() == "cityscapes":
        from support.cityscapes import SupportLayer
        use_coarse = True if args.extra is not None and \
                             "coarse" in args.extra[0].lower() \
                     else False
        support = SupportLayer(use_coarse)
    elif args.dataset[0].lower() == "freiburg":
        from support.freiburg import SupportLayer
        modalities = None if args.extra is None else args.extra
        support = SupportLayer(modalities)
    else:
        raise ValueError("Invalid argument \"dataset\": %s" % args.dataset[0])

    if os.path.exists(args.data_dir[0]):
        dataset_paths = support.file_associations(args.data_dir[0])
    else:
        raise ValueError("Dataset path does not exist\n%s\n" % args.data_dir[0])

    if not os.path.exists(args.output_dir[0]):
        sys.stdout.write("Directory \"%s\" does not exist. "
                         % args.output_dir[0])
        sys.stdout.write("Do you want to create it? [y/N] ")
        sys.stdout.flush()
        user_input = sys.stdin.read(1)
        if user_input.lower()[0] != "y":
            sys.exit(0)
        else:
            os.makedirs(args.output_dir[0])

    sess = tf.Session()
    for split in dataset_paths.keys():
        # Create directory for the split
        split_path = os.path.join(args.output_dir[0], split)
        if not os.path.exists(split_path):
            os.mkdir(split_path)
        try:
            p = multiprocessing.Pool(initializer=proc_initializer, initargs=[args, split_path])
            # Progress bar
            examples = list(dataset_paths[split].items())
            if show_progress:
                example_iter = tqdm(
                    p.imap_unordered(record_example,examples),
                    total=len(examples), desc="%-7s" % split).__iter__()
            else:
                example_iter = p.imap_unordered(examples)
            # retrieve a single prototype feature
            features = next(example_iter)
            # multiprocess the rest
            for _ in example_iter:
                pass
        finally:
            p.close()
    # Write feature keys in order to dynamically being able to reconstruct the
    # content of the records when reading the records.
    meta_file = os.path.join(args.output_dir[0], "meta.txt")
    with open(meta_file, "w") as f:
        f.write("\n".join(features.keys()))
    # In order to reconstruct:
    # features = {}
    # with open(meta_file, "r") as f:
    #   ln = f.readline()
    #   if ln.endswith("/data") or \
    #      ln.endswith("/encoding") or \
    #      ln == "label":
    #      features[ln] = tf.FixedLenFeature([], tf.string)
    #   else:
    #      features[ln] = tf.FixedLenFeature([], tf.int64)
    #
    # .../channels -> tf.FixedLenFeature([], tf.int64),
    # .../data     -> tf.FixedLenFeature([], tf.string),
    # .../encoding -> tf.FixedLenFeature([], tf.string),
    # label        -> tf.FixedLenFeature([], tf.string),
    # height       -> tf.FixedLenFeature([], tf.int64),
    # width        -> tf.FixedLenFeature([], tf.int64),


if __name__ == "__main__":
    # Setup commandline arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--data_root", \
                        type=str, \
                        nargs=1, \
                        dest="data_dir", \
                        required=True, \
                        help="Path to data set root directory.")
    parser.add_argument("-t", "--dataset", \
                        type=str, \
                        nargs=1, \
                        dest="dataset", \
                        required=True, \
                        help="Name of the dataset {cityscapes,freiburg,kitti}.")
    parser.add_argument("-o", "--output_dir", \
                        type=str, \
                        nargs=1, \
                        dest="output_dir", \
                        required=True, \
                        help="Path to where to store the records.")
    # TODO: make this a little nicer
    parser.add_argument(
        "-e", "--extra", type=str, nargs="*",
        dest="extra", required=False,
        help="Extra arguments. This is dependent on the particular dataset "
        "selected with the \"-t\" flag.\n"
        "%-10s : {gtCoarse} - coarsely annotated dataset.\n"
        "%-10s : {nir,nir_gray,depth_gray,(etc)} - additional modalities."
        % ("cityscapes", "freiburg"))
    args = parser.parse_args()
    main(args)