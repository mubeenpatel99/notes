from __future__ import print_function

import textwrap

import klip

MD_FMT = '* %s\n  %s'


def markdownize(clippings):
    for clip in clippings:
        wrapped = textwrap.wrap(clip['content'].strip(), width=78)
        wrapped_more = wrapped[1:]
        print(MD_FMT % (wrapped[0], '\n  '.join(wrapped_more)))
        if len(wrapped_more):
            print()

if __name__ == '__main__':
    import sys

    try:
        clippings_file = sys.argv[1]
    except IndexError:
        print("usage: %s <file_name>" % __file__, file=sys.stderr)
    else:
        clippings = klip.load_from_file(clippings_file, device="OldGenKindle")
        markdownize(clippings)