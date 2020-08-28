import nltk
from nltk.tokenize import sent_tokenize
from nltk.tokenize import word_tokenize
from nltk.corpus import stopwords
from nltk.stem import PorterStemmer

#insert comment!
#mytext = "Convert a primitive pyarrow.Array to a numpy array and boolean mask based on the buffers of the Array.\n\nParameters ---------- arr : pyarrow.Array dtype : numpy.dtype\n##### Returns\n"
mytext = "Draw histogram of the input series using matplotlib.\n\nParameters ---------- by : object, optional If passed, then used to form histograms for separate groups. ax : matplotlib axis object If not passed, uses gca(). grid : bool, default True Whether to show axis grid lines. xlabelsize : int, default None If specified changes the x-axis label size. xrot : float, default None Rotation of x axis labels. ylabelsize : int, default None If specified changes the y-axis label size. yrot : float, default None Rotation of y axis labels. figsize : tuple, default None Figure size in inches by default. bins : int or sequence, default 10 Number of histogram bins to be used. If an integer is given, bins + 1 bin edges are calculated and returned. If bins is a sequence, gives bin edges, including left edge of first bin and right edge of last bin. In this case, bins is returned unmodified. backend : str, default None Backend to use instead of the backend specified in the option ``plotting.backend``. For instance, 'matplotlib'. Alternatively, to specify the ``plotting.backend`` for the whole session, set ``pd.options.plotting.backend``.\n\n.. versionadded:: 1.0.0\n\n**kwargs To be passed to the actual plotting function.\n##### Returns\n* **matplotlib.axes.Axes.hist **: Plot a histogram using matplotlib.\n\n"

#tokenize and prepare stop words
stop_words = set(stopwords.words('english'))
word_tokens = word_tokenize(mytext)
filtered_sentence = [w for w in word_tokens if not w in stop_words]
filtered_sentence = []

#remove stop words
for w in word_tokens:
    if w not in stop_words:
        filtered_sentence.append(w)

#display tokenized list with stop words
print(word_tokens)

#display tokenized list
print(filtered_sentence)

#get rid of the annoying characters/symbols
cleanlist = []
for x in filtered_sentence:
    cleanlist.append(x.replace('-', '').replace('+', '').replace("'", '').replace('#', '').replace(':', '').replace('.', '').replace('*', '').replace('`', '').replace(',', '').replace('(', '').replace(')', ''))

while("" in cleanlist):
    cleanlist.remove("")

#display cleaned list
print(cleanlist)

#stem the words
words = cleanlist
ps = PorterStemmer()
for w in words:
    print(w, ":", ps.stem(w))

