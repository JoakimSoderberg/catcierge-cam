#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse

import zmq
import random
import sys
import time
import json


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--port", "-p", type=int, default=5556,
                    help="The zmq port to publish on")

    parser.add_argument("--topic", "-t", default="",
                    help="ZMQ topic")

    parser.add_argument("json",
                    help="The zmq port to publish on")

    args = parser.parse_args()

    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.bind("tcp://*:%s" % args.port)

    with open(args.json, "r") as f:
        data = f.read()
        print("send %s:\n%s" % (args.topic, data[:100]))
        socket.send("%s %s" % (args.topic, data))



if __name__ == '__main__': sys.exit(main())
