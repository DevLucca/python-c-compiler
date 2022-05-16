import core.parser.utils as p
import core.tree.nodes as nodes

from core.errors import error_collector
from core.parser.utils import (add_range, log_error, ParserError,
                                 raise_error)
from core.parser.declaration import parse_declaration

def parse(tokens_to_parse):
    p.best_error = None
    p.tokens = tokens_to_parse

    with log_error():
        return parse_root(0)[0]

    error_collector.add(p.best_error)
    return None


@add_range
def parse_root(index):
    items = []
    while True:
        with log_error():
            item, index = parse_declaration(index)
            items.append(item)
            continue

        break

    if not p.tokens[index:]:
        return nodes.Root(items), index
    else:
        raise_error("unexpected token", index, ParserError.AT)
