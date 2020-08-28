import ast
import os
import re
import json
import csv


def delete_space(parts, start, end):
    if start > end or end >= len(parts):
        return None
    count = 0
    while count < len(parts[start]):
        if parts[start][count] == ' ':
            count += 1
        else:
            break
    return '\n'.join(y for y in [x[count:] for x in parts[start : end + 1] if len(x) > count])


def change_args_to_dict(string):
    if string is None:
        return None
    ans = []
    strings = string.split('\n')
    ind = 1
    start = 0
    while ind <= len(strings):
        if ind < len(strings) and strings[ind].startswith(" "):
            ind += 1
        else:
            if start < ind:
                ans.append('\n'.join(strings[start:ind]))
            start = ind
            ind += 1
    d = {}
    for line in ans:
        if ":" in line and len(line) > 0:
            lines = line.split(":")
            d[lines[0]] = lines[1].strip()
    return d


def remove_next_line(comments):
    for x in comments:
        if comments[x] is not None and '\n' in comments[x]:
            comments[x] = ' '.join(comments[x].split('\n'))
    return comments


def skip_space_line(parts, ind):
    while ind < len(parts):
        if re.match(r'^\s*$', parts[ind]):
            ind += 1
        else:
            break
    return ind


# check if comment is None or len(comment) == 0 return {}
def parse_func_string(comment):
    if comment is None or len(comment) == 0:
        return {}
    comments = {}
    paras = ('Args', 'Attributes', 'Returns', 'Raises')
    comment_parts = [
        'short_description',
        'long_description',
        'Args',
        'Attributes',
        'Returns',
        'Raises',
    ]
    for x in comment_parts:
        comments[x] = None

    parts = re.split(r'\n', comment)
    ind = 1
    while ind < len(parts):
        if re.match(r'^\s*$', parts[ind]):
            break
        else:
            ind += 1

    comments['short_description'] = '\n'.join(
        ['\n'.join(re.split('\n\s+', x.strip())) for x in parts[0:ind]]
    ).strip(':\n\t ')
    ind = skip_space_line(parts, ind)

    start = ind
    while ind < len(parts):
        if parts[ind].strip().startswith(paras):
            break
        else:
            ind += 1
    long_description = '\n'.join(
        ['\n'.join(re.split('\n\s+', x.strip())) for x in parts[start:ind]]
    ).strip(':\n\t ')
    comments['long_description'] = long_description

    ind = skip_space_line(paras, ind)
    while ind < len(parts):
        if parts[ind].strip().startswith(paras):
            start = ind
            start_with = parts[ind].strip()
            ind += 1
            while ind < len(parts):
                if parts[ind].strip().startswith(paras):
                    break
                else:
                    ind += 1
            part = delete_space(parts, start + 1, ind - 1)
            if start_with.startswith(paras[0]):
                comments[paras[0]] = change_args_to_dict(part)
            elif start_with.startswith(paras[1]):
                comments[paras[1]] = change_args_to_dict(part)
            elif start_with.startswith(paras[2]):
                comments[paras[2]] = change_args_to_dict(part)
            elif start_with.startswith(paras[3]):
                comments[paras[3]] = part
            ind = skip_space_line(parts, ind)
        else:
            ind += 1

    remove_next_line(comments)
    return comments


def md_parse_line_break(comment):
    comment = comment.replace('  ', '\n\n')
    return comment.replace(' - ', '\n\n- ')


def to_md(comment_dict):
    doc = ''
    if 'short_description' in comment_dict:
        doc += comment_dict['short_description']
        doc += '\n\n'

    if 'long_description' in comment_dict:
        doc += md_parse_line_break(comment_dict['long_description'])
        doc += '\n'

    if 'Args' in comment_dict and comment_dict['Args'] is not None:
        doc += '##### Args\n'
        for arg, des in comment_dict['Args'].items():
            doc += '* **' + arg + '**: ' + des + '\n\n'

    if 'Attributes' in comment_dict and comment_dict['Attributes'] is not None:
        doc += '##### Attributes\n'
        for arg, des in comment_dict['Attributes'].items():
            doc += '* **' + arg + '**: ' + des + '\n\n'

    if 'Returns' in comment_dict and comment_dict['Returns'] is not None:
        doc += '##### Returns\n'
        if isinstance(comment_dict['Returns'], str):
            doc += comment_dict['Returns']
            doc += '\n'
        else:
            for arg, des in comment_dict['Returns'].items():
                doc += '* **' + arg + '**: ' + des + '\n\n'
    return doc


def parse_func_args(function):
    args = [a.arg for a in function.args.args if a.arg != 'self']
    kwargs = []
    if function.args.kwarg:
        kwargs = ['**' + function.args.kwarg.arg]

    return '(' + ', '.join(args + kwargs) + ')'


def get_func_comments(function_definitions, file_name):


    # intermediate and resultant dictionaries
    # intermediate
    dict2 = {}
    dict3 ={}
    # resultant
    dict1 = {}

    # fields in the sample file
    fields =['source file', 'line number', 'func name', 'func arg', 'comments']

    # loop variable
    i = 0

    # count variable for id creation
    l = 1

    for f in function_definitions:

        i = 0

        temp_str = to_md(parse_func_string(ast.get_docstring(f)))


        description = [file_name, str (f.lineno), f.name, parse_func_args(f), temp_str]



        # for automatic creation of id for each function


        while i<len(fields):

            # creating dictionary for each employee
            dict2[fields[i]]= description[i]

            i = i + 1

        # appending the record of each function to
        # the main dictionary
        dict1['function']= dict(dict2)
        dict3.update(dict1)
        dict1 = {}
        l = l + 1

#create json
    print(dict3)

    output_file_name = str(file_name + ".json")
    path = str("./output/")

    out_file = open(os.path.join(path, output_file_name), "w")

    with open(os.path.join(path, output_file_name), 'w'):
        json.dump(dict2, out_file, indent=4)
        out_file.close()


    # #creating json file
    #
    # print (dict3)
    #
    # output_file_name=str(file_name + ".json")
    # path = str("./output/")
    #
    # #json.dump(dict3, out_file, indent = 4)
    # #out_file.close()
    #
    # out_file = os.path.join(path, output_file_name)
    #
    # with open(out_file, 'w'):
    #     #out_file = open(output_file_name, "w")
    #     json.dump(dict3, out_file, indent=4)
    #     out_file.close()
    #
    # return 1


'''
    i = 0
    doc = ''
    i = i + 1

    print ('GET FUNC ')
    print (doc)
    for f in function_definitions:
        temp_str = to_md(parse_func_string(ast.get_docstring(f)))
        doc += ''.join(
            [
                '??',
                str(f.lineno),
                '### ',
                f.name.replace('_', '\\_'),
                '',
                '```python',
                '',
                'def ',
                f.name,
                parse_func_args(f),
                '',
                '```',
                '',
                temp_str,
                '',
            ]
        )

'''


def get_comments_str(file_name):
    with open(file_name) as fd:
        file_contents = fd.read()
    module = ast.parse(file_contents)
    function_definitions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    doc = get_func_comments(function_definitions, file_name)
    print(doc)

file = 'file_names.csv'
file_names2 = list()
print(file_names2)
with open(file, 'r') as this_csv_file:
    this_csv_reader = csv.reader(this_csv_file, delimiter=',')
    for rows in this_csv_reader:
        file_names2.append(rows)
        #''.join(map(str, file_names))
    #header = next(this_csv_reader)
print(file_names2)
file_names = [item for sublist in file_names2 for item in sublist]

#file_names = ('file_names.csv')
#file_names = file_names['files'].tolist()
#print(file_names)

def main():
    for file_name in file_names:
        print(file_name)
        get_comments_str(file_name)

    #file_name = 'conftest1.py'
    #get_comments_str(file_name)

if __name__ == "__main__":
    main()



