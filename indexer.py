import gzip
import json
import eventlet
import sys
from collections import defaultdict
from eventlet import pools
import mmap
httplib2 = eventlet.import_patched('httplib2')

cores = {
    'author'  : 'authors',
    'work'    : 'works',
    'subject' : 'subjects',
    'edition' : 'editions',
}

language_terms = {
    '[English]' : lambda key, text: (key + "_en", text),
    '[Deutsch]' : lambda key, text: (key + "_de", text)
}

def flatten_element(key, element):
    """A naive flatten that knowns (roughly) the shape of OL data """

    if element:
        if isinstance(element, dict):
            if 'type' in element:
                type = element['type']
                if type == '/type/datetime':
                    yield key, element['value'] + 'Z'
                elif type == '/type/link':
                    # TODO It would be nice to hold onto the title
                    yield key, element['url']
                elif type == '/type/text':
                    value = element['value']
                    if '\r' in value:
                        tokens = value.split('\r\n')
                        for token in tokens:
                            end_meta = token.rfind(']')
                            language = token[:end_meta]
                            if language in language_terms:
                                yield language_terms[language](token[end_meta + 1:])

            elif 'key' in element:
                assert len(element) == 1, "Unexpected number of elements found !"
                for _, value in flatten_element(key, element['key']):
                    yield key, value.split('/')[-1]
            else:
                for sub_k, sub_v in element.iteritems():
                    for sub_key, sub_element in flatten_element(sub_k, sub_v):
                        # HACK section titles ...
                        if sub_key != 'title':
                            yield sub_key, sub_element
        elif isinstance(element, list):
            for sub_element in element:
                for sub_key, sub_elem in flatten_element(key, sub_element):
                    yield sub_key, sub_elem
        elif isinstance(element, (unicode, str)):
            if element.startswith(u'/'):
                yield key, element.split('/')[-1]
            else:
                yield key, element.replace('"', r'\"')
        else:
            yield key, element

def reshape_record(record):
    """ Reworks the data to fit solr's idea about data """
    to_return = {}
    for k, v in record.iteritems():
        for key, value in flatten_element(k, v):
            if key in to_return:
                if not isinstance(to_return[key], list):
                    to_return[key] = [to_return[key], value]
                else:
                    to_return[key].append(value)
            else:
                to_return[key] = value
    return to_return

def process_lines(input):
    for line in input:
        tokens = line.split('\t')
        if tokens[0] != '/type/page':
            json_data = None
            try:
                json_data = json.loads(tokens[-1])
            except:
                print '\n'
                print '*' * 100
                print 'Bad row'
                print line
            if json_data:
                yield reshape_record(json_data)

def create_batches(iter, batch_size=500):
    while True:
        batches = defaultdict(list)
        for payload in iter:
            key = payload['type']
            batch = batches[key]
            batch.append(payload)
            if len(batch) >= batch_size:
                yield key, batch
                batches[key] = []

        for key, batch in batches.iteritems():
            yield key, batch

def index(server, key, batch, pool):
    with pool.item() as http:
        if key not in ('redirect'):
            url = '%s/%s/update' % (server, cores[key])

            resp, content = http.request(url, "POST", json.dumps(batch), \
                headers={'Content-type': 'application/json'})
            if resp.status >= 400:
                if len(batch) == 1:
                    print "\n"
                    print '*' * 100
                    print 'Bad Record'
                    print content
                    print batch
                else:
                    # Keep retrying the batch until we get down to the bad record
                    index(server, key, batch[::2], pool)
                    index(server, key, batch[1::2], pool)

                #sys.exit(1)

def index_ol(filename, server):
    pool = pools.Pool(max_size=256, create=lambda: httplib2.Http())
    pile = eventlet.GreenPile(size_or_pool=512)

    deletes = []
    moves = []

    with open(filename, 'rb') as handle:
        map = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        with gzip.GzipFile(mode='r', fileobj=map) as input:
            for key, batch in create_batches(process_lines(input), batch_size=50):
                if key == 'delete':
                    deletes.append(batch)
                elif key == 'redirect':
                    moves.append(batch)
                else:
                    pile.spawn(index, server, key, batch, pool)

    pile.waitall()

if __name__ == '__main__':
    index_ol(sys.argv[1], sys.argv[2])
