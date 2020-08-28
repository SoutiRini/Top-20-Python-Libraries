from __future__ import print_function
import json
import os
import csv
from nltk.stem import PorterStemmer
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize

#type name of library here
file_name = "tensorflow"

#load jsonnn
path = str("./jsons/")
with open(os.path.join(path, file_name + ".json")) as f:
    data = json.load(f)

#create dictionary based off of json (keys: func name, comments, stemmed comments)
count = 0
dict = {0: {'func name': [], 'comments': [], 'stemmed comments': []}}

for p in data['function']:
    dict[count] = {'func name': p['func name'], 'comments': p['comments'], 'stemmed comments': ''}
    count = count + 1

#print number of functions detected in library
print("Number of functions detected in " + file_name + " is " + str(count))

#NLTK: remove stop words, tokenize, remove unrelated characters, stem words -> add stem words to "stemmed comments" key in dictionary
count = 0
for x in dict:
    comments = (dict[count]['comments'])

    stop_words = set(stopwords.words('english'))
    word_tokens = word_tokenize(comments)
    filtered_sentence = [w for w in word_tokens if not w in stop_words]
    filtered_sentence = []
    for w in word_tokens:
        if w not in stop_words:
            filtered_sentence.append(w)
    cleanlist = []
    for x in filtered_sentence:
        cleanlist.append(
            x.replace('-', '').replace('+', '').replace("'", '').replace('#', '').replace(':', '').replace('.', '').replace('*', '').replace('`', '').replace(',', '').replace('(', '').replace(')', ''))
    while ("" in cleanlist):
        cleanlist.remove("")
    words = cleanlist
    ps = PorterStemmer()

    key_word_stem = []
    for w in words:
        key_words = set(ps.stem(w))
        key_word_stem.append(ps.stem(w))
        print(ps.stem(w))
    dict[count]['stemmed comments'] = key_word_stem

    count = count + 1

    #print(p['func name'])
    #print(p['comments'])
    #print('Comments: ' + p['comments'])

#remove "set()" discrepancy within commentless functions
count = 0
while count < 1346:
    unique_key_word = list(set(dict[count]["stemmed comments"]))
    dict[count]["stemmed comments"] = unique_key_word
    print(unique_key_word)
    count = count + 1

#IMPORTANT: rename the library here
##pandas_dict = dict

#csv output (disregard, not important)
w = csv.writer(open(file_name + ".csv", "w"))
for key, val in dict.items():
    w.writerow([key, val])