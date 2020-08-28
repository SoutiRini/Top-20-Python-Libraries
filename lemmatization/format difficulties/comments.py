#f = open('pandas_merged.json', "r")
#p = f.read()
#data = json.loads(p)

import json

data = {}
data['function'] = []
data['function'].append({
    'source file': 'debughelpers.py',
    'line number': '74',
    'func name': 'attach_enctype_error_multidict',
    'func arg': '(request)',
    'comments': "Since Flask 0.8 we're monkeypatching the files object in case a request is detected that does not use multipart form data but the files object is accessed.\n\n\n"
})
data['function'].append({
    'source file': 'debughelpers.py',
    'line number': '95',
    'func name': '_dump_loader_info',
    'func arg': '(loader)',
    'comments': ''
})
data['function'].append({
    'source file': 'debughelpers.py',
    'line number': '112',
    'func name': 'explain_template_loading_attempts',
    'func arg': '(app, template, attempts)',
    'comments': 'This should help developers understand what failed'
})
data['function'].append({
    'source file': 'debughelpers.py',
    'line number': '160',
    'func name': 'explain_ignored_app_run',
    'func arg': '',
    'comments': ''
})

with open ('mytest.json', 'w') as outfile:
     json.dump(data, outfile)




with open('mytest.json') as f:
    data = json.load(f)

for p in data['function']:
    print('Comments: ' + p['comments'])



#from nltk.corpus import stopwords
#from nltk.stem import WordNetLemmatizer
# #remove stop words
# stop_words = set(stopwords.words('english'))
# file1 = open("comments.txt")
# line = file1.read()
# words = line.split()
# for r in words:
#     if not r in stop_words:
#         appendFile = open('filteredcomments.txt', 'a')
#         appendFile.write(" " +r)
#         appendFile.close()
#
# lemmatizer = WordNetLemmatizer()
#
# print("rocks :", lemmatizer.lemmatize("rocks"))
# print("corpora :", lemmatizer.lemmatize("corpora"))
#
# # a denotes adjective in "pos"
# print("better :", lemmatizer.lemmatize("better", pos="a"))




